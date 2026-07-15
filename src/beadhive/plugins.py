"""plugins.py — the tiny generic plugin seam for external-tool integrations.

A **plugin** bundles a name, its own ``bh plugin <name> …`` Typer sub-app, an ``enabled``
predicate, and three optional lifecycle hooks the onboard / retire / rig-ready flows loop
over generically (so no integration is hardcoded by name into those modules). orca is the
first member; new integrations join by appending to ``registry()``.

Deliberately minimal (ponytail): ONE frozen dataclass + ONE static list. No dynamic
discovery, no entry-points — ``registry()`` returns a hand-written list, resolved lazily so a
plugin module can import ``plugins`` without an import cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer


@dataclass(frozen=True)
class Plugin:
    """One external-tool integration.

    ``enabled(cfg, entry)`` gates every lifecycle hook. The hooks are all optional and are
    each called inside a warn-and-continue fence by their caller, so a raising plugin never
    aborts onboarding / retire / rig-ready / worktree create-remove:

    - ``on_onboard(ctx)``               — wire the rig into the tool during onboarding.
    - ``on_retire(clone_path, cfg, entry)`` — notify on retire (WARN-only; no de-registration).
    - ``readiness(cfg, entry)``         — a ``(state, detail)`` pair for ``bh rig ready`` (or
      ``None`` when the entry lacks what the probe needs).
    - ``wt_create(cfg, entry, *, main, branch, target, start_point)`` — take over a worktree
      *create*; return the created ``Path``, or ``None`` if not handled (falls through to the
      native ``git worktree add``). bh's ``wt/`` branch-naming conventions stay authoritative —
      this only delegates the create subprocess, never the branch name.
    - ``wt_remove(cfg, entry, *, main, target, force, keep_branch)`` — take over a worktree
      *remove*; return ``True`` if handled, ``False`` if not (falls through to the native
      ``git worktree remove``). ``keep_branch`` is call-site intent (``True`` when the branch is
      the durable artifact, e.g. ``remove()``; ``False`` for ``prune()``, where the branch is
      already merged and disposable) — never a config knob.
    """

    name: str
    cli: typer.Typer
    enabled: Callable[[Any, Any], bool]
    on_onboard: Callable[[Any], None] | None = None
    on_retire: Callable[[Path | str, Any, Any], None] | None = None
    readiness: Callable[[Any, Any], tuple[str, str] | None] | None = None
    wt_create: Callable[..., Path | None] | None = None
    wt_remove: Callable[..., bool] | None = None


def registry() -> list[Plugin]:
    """The static list of registered plugins.

    Import-safe: each plugin module is imported lazily inside this function (a plugin module
    imports ``plugins`` for the ``Plugin`` type, so a module-level import here would cycle).
    git-workspace is listed first — orca's own ``enabled`` (``config.orca_enabled``) AND-gates
    on ``git_workspace.enabled``, so it logically depends on this plugin. New integrations join
    the list the same way.
    """
    from . import (
        gitworkspace_plugin,  # lazy: avoid an import cycle
        orca,  # lazy: avoid the plugins <-> orca import cycle
    )

    return [gitworkspace_plugin.PLUGIN, orca.PLUGIN]
