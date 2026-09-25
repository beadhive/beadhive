"""Hive builder: a real main clone under $GIT_WORKSPACE with embedded bd + a filesystem
git remote and a filesystem dolt remote, registered in the ws config.

Identity + signing are written **repo-local** so ws-driven git ops (which scrub
GIT_CONFIG_GLOBAL) still see them; the supervised modality inherits this human identity.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import uuid
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


def _enable_batch_writes(beads_dir: Path) -> None:
    with (beads_dir / "config.yaml").open("a") as stream:
        stream.write('\ndolt.auto-commit: "batch"\n')


def empty_beads_template(shared_root: Path, prefix: str = "mr") -> Path:
    """Publish one immutable empty embedded-bd store for all AGF cases in this pytest run.

    Xdist workers share ``shared_root``. A file lock serializes the one real ``bd init`` and an
    atomic rename publishes only a complete template. Every test deep-copies the result, so no
    command ever opens the template database itself.
    """
    shared_root.mkdir(parents=True, exist_ok=True)
    template = shared_root / f"empty-beads-{prefix}"
    lock_path = shared_root / f"empty-beads-{prefix}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not template.is_dir():
            staging = shared_root / f".empty-beads-{prefix}-{os.getpid()}-{uuid.uuid4().hex}"
            staging.mkdir()
            git("init", "-q", "-b", "main", cwd=staging)
            beads.init_embedded(staging, prefix)
            _enable_batch_writes(staging / ".beads")
            os.replace(staging / ".beads", template)
            shutil.rmtree(staging)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return template


def template_digest(template: Path) -> str:
    """Content identity used to prove matrix cases never mutate their shared template."""
    digest = hashlib.sha256()
    for path in sorted(item for item in template.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(template)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _clone_beads_template(template: Path, destination: Path) -> None:
    # The metadata project id is the database's embedded identity and must move with its cloned
    # storage. Each destination is a private deep copy and never connects to a shared server.
    shutil.copytree(template, destination)


def _configure_human_identity(main: Path, world: World) -> None:
    """Write the fixed fixture identity in one immutable setup write.

    Six separate ``git config`` processes previously rewrote this same repo-local file before
    any workflow command could observe it. Git's own parser remains the consumer and every
    history/signature assertion below proves the resulting configuration.
    """
    human = world.human
    with (main / ".git" / "config").open("a") as stream:
        stream.write(
            "[user]\n"
            f"\tname = {human.name}\n"
            f"\temail = {human.email}\n"
            f"\tsigningkey = {human.key}\n"
            "[gpg]\n"
            "\tformat = ssh\n"
            "[commit]\n"
            "\tgpgsign = true\n"
            '[gpg "ssh"]\n'
            f"\tallowedSignersFile = {world.allowed}\n"
        )


def make_hive(
    world: World,
    *,
    org="myorg",
    repo="myrepo",
    prefix="mr",
    work=None,
    chdir=True,
    with_remotes=False,
    batch_bd_writes=False,
    beads_template: Path | None = None,
) -> Hive:
    """Build a hive. `with_remotes=True` also wires a bare git remote + a file:// dolt remote
    and publishes to them — needed ONLY by the remote-sandbox modality. The matrix modalities
    work entirely in linked worktrees, so they skip remotes (avoids a per-hive `bd dolt`
    round-trip through the shared dolt sql-server, which is what stalled the suite)."""
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)

    # repo-local human identity + ssh signing + allowed-signers (inherited by supervised worktrees)
    _configure_human_identity(main, world)

    (main / "README.md").write_text("# hive\n")
    # bd init/bootstrap drop .beads/ + AGENTS.md etc. into the working dir; ignore them like a
    # real hive so a full-clone developer's `git add -A` never commits beads internals.
    (main / ".gitignore").write_text(".beads/\nAGENTS.md\nCLAUDE.md\n.codex/\n")
    git("add", "-A", cwd=main)
    git("commit", "-qm", "chore: init", cwd=main)

    if beads_template is None:
        beads.init_embedded(main, prefix)
    else:
        _clone_beads_template(beads_template, main / ".beads")
    if batch_bd_writes and beads_template is None:
        # Every matrix case owns one isolated database and drives it sequentially. Deferring
        # Dolt commits avoids paying a durable commit for every intermediate fixture mutation;
        # the complete working set remains visible to each following real-bd process.
        _enable_batch_writes(main / ".beads")

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
