"""Derive a repo's (provider, org, repo) identity from its git-workspace path, and resolve the
one root bh clones/manages repos under.

Shared by `issue create` (triplet labels), `hive init` (registration), and — via
`workspace_root()` — effectively everything else that touches a clone on disk: survey, orca,
metadata, retire, archive, worktree, `config.archive_dir`/`_marketplace_root`, and the
git-workspace plugin. `workspace_root()` is their ONE choke point; a mode/precedence change
belongs here and nowhere else, or the two modes drift apart.

Resolution precedence (bh-cgcg):
  1. `$GIT_WORKSPACE`                  — explicit env always wins (escape hatch / testing).
  2. config `git_workspace.mode`/`.root` — an explicit opt-in: `internal` (bh-owned,
     `<bh home>/ws`) or `external` (the user's existing git-workspace — today's `~/workspace`
     default); an explicit `root` wins over either mode.
  3. internal default (`<bh home>/ws`, a sibling of the worktrees shadow tree at
     `<bh home>/wt`) — UNLESS an existing `~/workspace` is already populated (real clones or
     registered hives), in which case THAT wins instead: flipping the default must never
     silently relocate an existing install (the legacy guard).
"""

from __future__ import annotations

import os
from pathlib import Path

from .run import run


def _legacy_root() -> Path:
    """The pre-bh git-workspace default root: `~/workspace` — orf/git-workspace's own
    fallback, independent of `$BH_HOME`. A function (not a module-level constant) so tests
    can monkeypatch it straight to a tmp dir instead of touching the real `$HOME`."""
    return Path.home() / "workspace"


def _internal_root() -> Path:
    """The bh-owned internal default: `<bh home>/ws` — a sibling of the worktrees shadow
    tree (`<bh home>/wt`), and always derived from the resolved bh home (`config.home()`,
    `$BH_HOME`/`$BH_CONFIG`-aware) rather than a hardcoded `~`."""
    from . import config  # function-level: avoid a config<->identity import cycle

    return config.home() / "ws"


def _legacy_workspace_populated(root: Path) -> bool:
    """True when `root` holds real content that predates the internal-default flip — an
    actual clone (`provider/org/repo/.git`, matching git-workspace's own on-disk layout) or a
    hive already registered in `managed_repos` — never merely that the directory exists. An
    empty `~/workspace` left over from a previous experiment must not pin a fresh install to
    external mode forever.

    The `managed_repos` check is SCOPED to `root`: a registered hive only counts as
    populating `root` when its derived clone path (`config.managed_repo_path` — the same
    `root/provider/org/repo` derivation `config.py`'s `_marketplace_root` uses) actually
    exists there. A hive registered while `workspace_root()` resolved to somewhere else
    (e.g. the internal default) must NOT make `root` look populated — otherwise onboarding a
    single hive under a fresh internal install would flip every later call to legacy external,
    which is precisely the silent-relocation this guard exists to prevent."""
    if root.is_dir() and any(root.glob("*/*/*/.git")):
        return True
    from . import config  # function-level: avoid a config<->identity import cycle

    try:
        cfg = config.load()
    except FileNotFoundError:
        return False
    return any(
        (config.managed_repo_path(root, entry) / ".git").is_dir()
        for entry in config.managed_repos(cfg)
    )


def workspace_root() -> str:
    """The root bh clones/manages repos under — see the module docstring for the 3-tier
    precedence this implements."""
    root = os.environ.get("GIT_WORKSPACE")
    if not root:
        from . import config  # function-level: avoid a config<->identity import cycle

        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = {}
        gw = cfg.get("git_workspace") or {}
        mode = gw.get("mode")
        override = gw.get("root")
        legacy = _legacy_root()
        if override:
            root = override
        elif mode == "internal":
            root = str(_internal_root())
        elif mode == "external":
            root = str(legacy)
        elif _legacy_workspace_populated(legacy):
            root = str(legacy)
        else:
            root = str(_internal_root())
    try:
        return str(Path(root).expanduser().resolve())
    except OSError:
        return os.path.expanduser(root)


def workspace_mode(root: str | None = None) -> str:
    """Classify `root` (default: the resolved `workspace_root()`) as ``"internal"`` or
    ``"external"`` — never a second root derivation, just a comparison of what
    `workspace_root()` already resolved to against the SAME `_internal_root()` helper it uses
    internally. ``"internal"`` iff `root` IS the bh-owned default (`<bh home>/ws`); anything
    else (an env override, an explicit `external` config, a custom `root` override, or the
    legacy-populated guard falling back to `~/workspace`) is ``"external"``.

    Setup code (`bh config init`, `bh doctor`) uses this to decide whether it owns the root
    (safe to create/seed) or must leave someone else's existing workspace untouched."""
    root = root if root is not None else workspace_root()
    candidate = Path(root).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        pass
    return "internal" if candidate == _internal_root().resolve() else "external"


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
        # Agent identity with no key: pin signing OFF so the agent doesn't inherit the
        # human's global commit.gpgsign and sign with their key under the agent's name.
        _wt("commit.gpgsign", "false")
