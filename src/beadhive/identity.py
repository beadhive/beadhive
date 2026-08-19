"""Derive a repo's (provider, org, repo) identity from its git-workspace path.

Shared by `issue create` (triplet labels) and `hive init` (registration). The
workspace root is $GIT_WORKSPACE (default ~/workspace); a repo's path under it is
<provider>/<org>/.../<repo>.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

from .run import run


def workspace_root() -> str:
    # A BLANK `GIT_WORKSPACE` is an empty shell variable, not an operator asking for the empty
    # path — `.get(name, default)` returns "" for it, and `Path("").resolve()` is the CWD, so
    # every reader downstream would silently take whichever directory bh happened to be run
    # from. Blank is unset here, matching how `credentials._env_source` reads every other
    # environment credential (bh-9qor).
    root = os.environ.get("GIT_WORKSPACE", "").strip() or str(Path.home() / "workspace")
    try:
        return str(Path(root).expanduser().resolve())
    except OSError:
        return os.path.expanduser(root)


@cache
def workspace_identity(cwd=None):
    """Return (provider, org, repo), or None when outside a managed workspace path.

    MEMOIZED PER PROCESS (bh-z31lc). This forks `git rev-parse --show-toplevel` and was the
    single most-repeated git call on the read path — 29 spawns in one `bh doctor`, most of
    them re-asking about a directory already asked about. A directory's git toplevel does not
    change while bh runs: bh never `os.chdir`s (checked — every verb threads `cwd=` instead),
    and a repo would have to be moved or re-inited underneath a live process to invalidate an
    entry.

    ponytail: process-lifetime cache with no invalidation. Correct for the CLI, where a
    process is one verb. A long-lived host daemon that outlives a `git init` would need
    `workspace_identity.cache_clear()` on that event — there is no such daemon today.
    """
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


def _env_actor() -> str:
    """The seat identity from the environment: `$BH_DEV` (canonical) with `$WS_DEV` and the
    older `$WS_CREW` kept as DEPRECATED aliases, in that fallback order. `BH_DEV` wins when
    set; a bare `WS_DEV`/`WS_CREW` still resolves but emits a one-line deprecation warning
    (removed later per the limn/kkke migration sequencing). Returns '' when none are set."""
    bh_dev = os.environ.get("BH_DEV")
    if bh_dev:
        return bh_dev
    dev = os.environ.get("WS_DEV")
    if dev:
        from . import log  # lazy: identity is imported early; avoid a hard log dependency

        log.get_logger(__name__).warning(
            "deprecated_env_var",
            old="WS_DEV",
            new="BH_DEV",
            hint="set BH_DEV instead — WS_DEV support will be removed later",
        )
        return dev
    crew = os.environ.get("WS_CREW")
    if crew:
        from . import log  # lazy: identity is imported early; avoid a hard log dependency

        log.get_logger(__name__).warning(
            "ws_crew_env_deprecated",
            deprecated="WS_CREW",
            replacement="BH_DEV",
            reason="seat env renamed per the roles/RBAC matrix (crew/ -> dev/) and the bh rebrand",
        )
        return crew
    return ""


def resolve_actor(explicit: str = "", profile_name: str = "", cwd=None) -> str:
    """The seat identity for `bd --actor` and git author.
    Precedence: explicit `--as` > config profile name > $BH_DEV (or deprecated $WS_DEV /
    $WS_CREW) > git user.name > $USER."""
    for cand in (explicit, profile_name, _env_actor()):
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
        _stamp_host_key(_wt)


def _stamp_host_key(_wt) -> None:
    """Signing for an agent seat that has no key OF ITS OWN (bh-y3lp).

    This branch used to pin ``commit.gpgsign=false``, and that reasoning was sound in
    isolation — inheriting the human's global signing would sign with THEIR key under the
    AGENT's name, which is its own, worse, integrity problem. The defect was that the
    alternative it chose collides head-on with a branch rule requiring every commit to be
    signed: a worktree-scoped ``false`` OVERRIDES the human's global ``true``, so every commit
    made in a stamped worktree was unsigned BY CONSTRUCTION, and the merge gate (or, when that
    gate is off, GitHub at push time — 31 commits later) is the first thing to notice.

    Both of those options are wrong. This is the third one the bead names: sign with the
    **host's** key — ``host.yaml``'s per-host ``signing_key``, the key
    :mod:`beadhive.git_identity` publishes into HQ's ``allowed_signers`` and therefore the one
    the fleet already verifies as TRUSTED. The commit is attributed to the seat and signed by
    the machine the seat ran on, which is exactly what happened, and it is signed by
    CONSTRUCTION rather than by whatever a laptop's global config happened to carry.

    A host with no recorded key keeps the original pin: it cannot sign as itself, so falling
    back to the human's key is still the worse trade. That state is loud elsewhere — `bh host
    identity` marries the halves, and `host_provision.status` reports it as a first-class
    check — rather than silently signed under the wrong identity here."""
    from . import host  # lazy: identity is imported early; keep host.yaml IO off that path

    try:
        key = host.signing_key()
    except Exception:  # noqa: BLE001 — an unminted/unreadable host.yaml is "no key", not an error
        key = ""
    if not key:
        _wt("commit.gpgsign", "false")
        return
    _wt("gpg.format", "ssh")
    _wt("user.signingkey", os.path.expanduser(key))
    _wt("commit.gpgsign", "true")
