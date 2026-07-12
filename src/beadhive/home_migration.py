"""One-time ~/.ws -> ~/.beadhive home-dir migration, split out of config.py.

Self-contained cluster: the directory move plus the two follow-on repairs a bare move can't
do for free — rewriting config values that textually hardcode the old home path (e.g. a
customized ``worktrees.path``), and re-linking every persistent worktree's git bookkeeping so
it isn't left ``prunable``. The ``_DEFAULT_HOME_OLD``/``_DEFAULT_HOME_NEW`` path constants and
the load/save/get/set/worktrees helpers stay in :mod:`config` (``home()`` needs the new-home
constant), so this module reads them through the ``config`` module object — a test
monkeypatching ``config._DEFAULT_HOME_OLD``/``_NEW`` still steers the migration.
"""

from __future__ import annotations

import shutil
from collections.abc import MutableMapping
from pathlib import Path

from . import config


def _home_migrated() -> bool:
    """Whether the new home is a genuine migrated (or deliberately fresh) install, vs. a stray
    artifact some code path wrote before migration ever ran. Bare directory
    existence isn't enough — a fixture cache dir or a setup-state.json is not proof of a real
    install, but a ``config.yaml`` is: nothing writes one except the operator (``config init``)
    or a completed move (which brings the old one across)."""
    return (config._DEFAULT_HOME_NEW / "config.yaml").exists()


def _stale_home_path_keys(cfg, old_home: Path) -> list[str]:
    """Dotted keys in ``cfg`` whose string value is textually rooted under ``old_home`` — e.g.
    an operator-set ``worktrees.path: ~/.ws/wt``. A directory move can't fix these: they're just
    text, unrelated to the filesystem until something re-reads and expands them."""
    prefixes = (str(old_home), f"~/{old_home.name}")
    found: list[str] = []

    def walk(node, prefix: str) -> None:
        if isinstance(node, MutableMapping):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, str) and any(node.startswith(p) for p in prefixes):
            found.append(prefix)

    walk(cfg, "")
    return found


def _rewrite_stale_home_paths(cfg, old_home: Path, new_home: Path) -> list[str]:
    """Rewrite every stale-home-path config value in place (old_home prefix -> new_home prefix),
    preferring the ``~/<name>`` form so the rewritten value stays portable. Returns the dotted
    keys changed; caller decides whether/how to persist. Best-effort: a key set() never raises
    hard enough to abort the caller's own best-effort wrapper."""
    changed = []
    for key in _stale_home_path_keys(cfg, old_home):
        old_val = str(config.get_value(key, cfg)["value"])
        new_val = old_val.replace(str(old_home), str(new_home)).replace(
            f"~/{old_home.name}", f"~/{new_home.name}"
        )
        config.set_value(key, new_val, cfg=cfg)
        changed.append(key)
    return changed


def _repair_worktrees_after_move(cfg, new_home: Path) -> list[str]:
    """Git's own worktree bookkeeping stores absolute paths on both sides (the main repo's
    ``.git/worktrees/<name>/gitdir`` and the linked worktree's own ``.git`` file), so moving the
    home dir — and every persistent worktree under it — leaves each affected repo's
    ``git worktree list`` reporting every entry ``prunable`` until repaired. Walks the moved
    worktrees tree and runs ``git worktree repair`` once per owning repo (batched, not one call
    per worktree). Returns the repo paths repaired. Best-effort throughout: a repo this can't
    resolve (unregistered, moved, whatever) is simply left for a manual `git worktree repair`."""
    from .identity import workspace_root
    from .run import run

    wt_root = config.worktrees_root(cfg)
    try:
        wt_root.relative_to(new_home)
    except ValueError:
        return []  # worktrees live outside the home dir (ephemeral/OS-temp, or unaffected)
    if not wt_root.is_dir():
        return []

    ws_root = Path(workspace_root())
    by_repo: dict[str, list[str]] = {}
    for leaf in wt_root.glob("*/*/*/*"):
        if not leaf.is_dir():
            continue
        provider, org, repo = leaf.parts[-4:-1]
        main_repo = ws_root / provider / org / repo
        if main_repo.is_dir():
            by_repo.setdefault(str(main_repo), []).append(str(leaf))

    repaired = []
    for main_repo, paths in by_repo.items():
        res = run(["git", "worktree", "repair", *paths], cwd=main_repo, check=False)
        if res.returncode == 0:
            repaired.append(main_repo)
    return repaired


def migrate_home_if_needed() -> None:
    """One-time move of the pre-rebrand ~/.ws/ to ~/.beadhive/, including
    the two follow-on repairs a bare directory move can't do for free:
    rewriting config values that textually hardcode the old home path (e.g. a customized
    ``worktrees.path``), and re-linking every persistent worktree's git bookkeeping so it isn't
    left `prunable`.

    Deliberately NOT called from ``home()`` or any other getter: a plain read must never have
    the side effect of moving real state on disk, or every import/test/library call becomes a
    latent mutation hazard. Call this exactly once, from the one place that represents a real,
    intentional CLI invocation (``cli._root``) — never from a test or library import path.

    Only fires on the fully-default path — an explicit BH_HOME or legacy WS_HOME means the
    operator already made a deliberate choice, so migration stays out of the way. A genuinely
    migrated (or deliberately fresh) new home is a cheap no-op check (``_home_migrated``); a
    *stray* new home (no ``config.yaml`` — some code path wrote a cache file before migration
    ever ran) is cleared first so the real move isn't silently skipped forever."""
    if config._env("home") is not None or not config._DEFAULT_HOME_OLD.is_dir():
        return
    if _home_migrated():
        return
    if config._DEFAULT_HOME_NEW.exists():
        shutil.rmtree(config._DEFAULT_HOME_NEW)
    shutil.move(str(config._DEFAULT_HOME_OLD), str(config._DEFAULT_HOME_NEW))

    from . import log  # lazy: keep config free of the log<->config import cycle

    logger = log.get_logger(__name__)
    rewritten: list[str] = []
    repaired: list[str] = []
    try:
        cfg = config.load()
        rewritten = _rewrite_stale_home_paths(
            cfg, config._DEFAULT_HOME_OLD, config._DEFAULT_HOME_NEW
        )
        if rewritten:
            config.save(cfg)
        repaired = _repair_worktrees_after_move(cfg, config._DEFAULT_HOME_NEW)
    except Exception as exc:  # best-effort: the directory move already succeeded either way
        logger.warning("home_dir_migration_followup_failed", error=str(exc))

    logger.warning(
        "home_dir_migrated",
        old=str(config._DEFAULT_HOME_OLD),
        new=str(config._DEFAULT_HOME_NEW),
        rewritten_config_keys=rewritten,
        repaired_repos=repaired,
    )
