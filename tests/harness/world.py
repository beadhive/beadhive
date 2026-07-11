"""Isolated AGF test world: tmp roots, env wiring, ephemeral signing keys + allowed_signers.

A `World` carves out a hermetic sandbox: its own $GIT_WORKSPACE, bh home/config/worktrees,
an empty global git config (so the real ~/.gitconfig never leaks), and a `keys/` dir of
ephemeral ed25519 signing keys with a cumulative allowed_signers file. Identity + signing
config is written **repo-local** by the rig builder (bh's own git calls scrub GIT_* incl.
GIT_CONFIG_GLOBAL, so global config is unreliable for bh-driven ops — repo-local always wins).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from beadhive.run import run


def progress(msg: str):
    """Live, flushed progress to stderr — only when AGF_RENDER is set (so a normal
    `just test-int` stays quiet, but `just render-int` streams what's happening)."""
    if os.environ.get("AGF_RENDER"):
        print(msg, file=sys.stderr, flush=True)

# Keep the harness's own git calls isolated: drop only the dir-pointing GIT_* vars (which would
# override `-C`), but KEEP GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM so no real user config leaks.
_DROP = {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}


def git_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _DROP}


def git(*args, cwd=None, check=True):
    res = run(
        ["git", *args], cwd=str(cwd) if cwd else None, check=False, capture=True, env=git_env()
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} → {res.returncode}\n{res.stdout}\n{res.stderr}"
        )
    return res


@dataclass(frozen=True)
class Identity:
    """An author/committer identity, optionally with an ed25519 signing key (private path)."""

    name: str
    email: str
    key: Path | None = None  # private key path; None → unsigned

    @property
    def pub(self) -> Path | None:
        return Path(str(self.key) + ".pub") if self.key else None


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = Path(tmp_path)
        self.ws_root = self.tmp / "workspace"  # $GIT_WORKSPACE
        self.wts = self.tmp / "wts"  # $BH_WORKTREES
        self.home = self.tmp / "wshome"  # $BH_HOME
        self.keys = self.tmp / "keys"
        self.remotes = self.tmp / "remotes"
        self.sandboxes = self.tmp / "sandboxes"
        self.cfg_path = self.tmp / "config.yaml"
        self.allowed = self.tmp / "allowed_signers"
        self.gitconfig = self.tmp / "gitconfig"  # $GIT_CONFIG_GLOBAL
        for d in (self.ws_root, self.keys, self.remotes, self.sandboxes):
            d.mkdir(parents=True, exist_ok=True)
        self.allowed.write_text("")
        # core.excludesFile=/dev/null: git falls back to $XDG_CONFIG_HOME/git/ignore for the
        # global excludes file independent of this config's own contents, so a developer's
        # personal ignore rules (e.g. a `.beads/` rule) would otherwise leak into git calls
        # meant to be hermetic. Pin it here rather than relying on GIT_CONFIG_GLOBAL being empty.
        self.gitconfig.write_text("[core]\n\texcludesFile = /dev/null\n")

        for k, v in {
            "GIT_WORKSPACE": str(self.ws_root),
            "BH_WORKTREES": str(self.wts),
            "BH_HOME": str(self.home),
            "BH_CONFIG": str(self.cfg_path),
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            # Never paginate or prompt: under `pytest -s` the subprocesses inherit the real
            # TTY, and a bd/git pager (less) would block forever waiting for a keypress.
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "BD_NON_INTERACTIVE": "1",
            "NO_COLOR": "1",
        }.items():
            monkeypatch.setenv(k, v)
        # Force isolated embedded bd: drop anything that would redirect it at a shared server.
        _prefixed = (k for k in os.environ if k.startswith(("BEADS_", "DOLT_")))
        for k in (*_prefixed, "BH_CREW", "BH_DEV", "WS_CREW", "WS_DEV"):
            monkeypatch.delenv(k, raising=False)
        self._monkeypatch = monkeypatch

        self.cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
        # The fabricated human (supervised modality) and the merge owner (Refiner).
        self.human = self.identity("Human Dev", "human@fixture", sign=True)
        self.refiner = self.identity("Refiner", "refiner@fixture", sign=True)

    def identity(self, name: str, email: str, sign: bool = True) -> Identity:
        """Make an Identity; when sign=True, generate an ed25519 key and register it as an
        allowed signer for `email` so git verification yields a good ('G') signature."""
        key = None
        if sign:
            key = self.keys / f"{email.replace('@', '_at_')}"
            if not key.exists():
                run(
                    ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", email, "-f", str(key), "-q"],
                    check=True,
                    capture=True,
                )
            pub = Path(str(key) + ".pub").read_text().strip()
            with self.allowed.open("a") as f:
                f.write(f'{email} namespaces="git" {pub}\n')
        return Identity(name, email, key)

    def chdir(self, path: Path):
        self._monkeypatch.chdir(path)
