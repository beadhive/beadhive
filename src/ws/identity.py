"""Derive a repo's (provider, org, repo) identity from its git-workspace path.

Shared by `issue create` (triplet labels) and `rig init` (registration). The
workspace root is $GIT_WORKSPACE (default ~/workspace); a repo's path under it is
<provider>/<org>/.../<repo>.
"""

from __future__ import annotations

import os
from pathlib import Path

from .run import run


def workspace_root() -> str:
    root = os.environ.get("GIT_WORKSPACE", str(Path.home() / "workspace"))
    try:
        return str(Path(root).expanduser().resolve())
    except OSError:
        return os.path.expanduser(root)


def workspace_identity(cwd=None):
    """Return (provider, org, repo), or None when outside a managed workspace path."""
    res = run(["git", "rev-parse", "--show-toplevel"], check=False, capture=True, cwd=cwd)
    if res.returncode != 0:
        return None
    top = res.stdout.strip()
    root = workspace_root()
    if not top.startswith(root + os.sep):
        return None
    parts = top[len(root) + 1 :].split("/")
    if len(parts) < 3:
        return None
    # provider/org/.../repo — provider first, org second, repo last (matches bdc).
    return parts[0], parts[1], parts[-1]


# ---- per-agent identity + commit signing (for `ws work`) --------------------


def resolve_actor(explicit: str = "", profile_name: str = "", cwd=None) -> str:
    """The crew identity for `bd --actor` and git author.
    Precedence: explicit `--as` > config profile name > $WS_CREW > git user.name > $USER."""
    for cand in (explicit, profile_name, os.environ.get("WS_CREW")):
        if cand:
            return cand
    res = run(["git", "config", "user.name"], check=False, capture=True, cwd=cwd)
    name = (res.stdout or "").strip() if res.returncode == 0 else ""
    return name or os.environ.get("USER", "unknown")


def stamp(target, name="", email="", signing_key="", sign=False) -> None:
    """Stamp per-worktree git config: author identity, plus SSH commit signing when a key is
    given. Called at claim/assign in *agent* mode. *Supervised* mode passes no key (and the
    caller skips this entirely), so the worktree inherits the human's existing signing setup.

    Writes are **worktree-scoped** (`extensions.worktreeConfig` + `--worktree`): linked
    worktrees otherwise share `$GIT_DIR/config`, so two agents in sibling worktrees would
    clobber each other's identity. With this, each worktree carries its own."""
    # Enabling worktreeConfig is on the shared config (idempotent) — required before --worktree.
    run(["git", "-C", str(target), "config", "extensions.worktreeConfig", "true"], check=False)

    def _wt(*kv):
        run(["git", "-C", str(target), "config", "--worktree", *kv], check=False)

    if name:
        _wt("user.name", name)
    if email:
        _wt("user.email", email)
    if signing_key:
        _wt("gpg.format", "ssh")
        # ~ expands a key *path*; a literal "ssh-ed25519 …" value is left untouched.
        _wt("user.signingkey", os.path.expanduser(signing_key))
        _wt("commit.gpgsign", "true" if sign else "false")
    else:
        # Agent identity with no key: pin signing OFF so the agent doesn't inherit the
        # human's global commit.gpgsign and sign with their key under the agent's name.
        _wt("commit.gpgsign", "false")
