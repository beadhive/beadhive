"""Root-supplied ``CommandRunner`` adapters over ``run``/``missing_binary``/``cache_locality``
(bh-qdezo.6).

The concrete ports :mod:`beadhive_worktrees.policy.init_rules`'s pure policy depends on.
Composition, never policy: each method is a thin, behavior-preserving pass-through to the
pre-existing argv-era calls, so the package's rule-evaluation/fingerprint-stamp logic never
spawns a real subprocess in its own tests.

Every method resolves its collaborator (``run``, ``missing_binary``, ``cache_locality``,
``config``) through :mod:`beadhive.worktree_verify`'s own facade seams at call time, so the
existing patch points (``worktree.run``, ...) keep intercepting these calls unchanged.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beadhive_worktrees import CommandOutcome

from . import cache_locality
from .config_consumer_ports import work_settings as config


def _facade():
    # ``importlib.import_module`` (not a plain ``from . import worktree_verify``): this file is
    # imported BY ``worktree_verify`` to compose its adapters, so a plain import back would
    # close a static cycle. ``worktree_verify.py`` already resolves ITS own call-back to
    # ``worktree.py`` the same way (see its ``_facade()``); this mirrors that established seam.
    return importlib.import_module(".worktree_verify", __package__)


@dataclass
class HostInitCommandRunner:
    """``CommandRunner`` for init-rule commands: cache-locality-aware, best-effort."""

    cfg: Any

    def run(self, argv: list[str], *, cwd: Path) -> CommandOutcome:
        facade = _facade()
        cache = cache_locality.command_environment(
            argv,
            cwd,
            worktree_root=config.worktrees_root(self.cfg),
            ephemeral=config.worktrees_ephemeral(self.cfg),
        )
        run_kwargs: dict[str, Any] = {"cwd": str(cwd), "check": False}
        notes: list[str] = []
        warnings: list[str] = []
        if cache is not None:
            child_env, selection = cache
            run_kwargs["env"] = child_env
            notes.append(
                f"  → cache[{selection.application}] {selection.tier} "
                f"{selection.path} ({selection.link_method}; "
                f"device {selection.cache_device} → {selection.target_device}; "
                f"{selection.free_bytes} bytes/{selection.free_inodes} inodes free)"
            )
            if selection.link_method == "copy" or "fallback" in selection.diagnostic:
                warnings.append(f"  ⚠ cache locality: {selection.diagnostic}")
        res = facade.run(argv, **run_kwargs)
        return CommandOutcome(
            returncode=res.returncode,
            missing=facade.missing_binary(res),
            stdout=getattr(res, "stdout", "") or "",
            notes=tuple(notes),
            warnings=tuple(warnings),
        )


class HostGitConfigRunner:
    """``CommandRunner`` for the init-rules Git worktree-local config stamp (plain, no cache
    locality — these are ``git config`` reads/writes, not provisioning commands)."""

    def run(self, argv: list[str], *, cwd: Path) -> CommandOutcome:
        del cwd  # `-C <path>` is already baked into argv by the caller
        res = _facade().run(argv, check=False, capture=True)
        return CommandOutcome(returncode=res.returncode, stdout=(res.stdout or ""))


__all__ = ["HostGitConfigRunner", "HostInitCommandRunner"]
