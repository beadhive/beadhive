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
    aborts onboarding / retire / rig-ready:

    - ``on_onboard(ctx)``               — wire the rig into the tool during onboarding.
    - ``on_retire(clone_path, cfg, entry)`` — notify on retire (WARN-only; no de-registration).
    - ``readiness(cfg, entry)``         — a ``(state, detail)`` pair for ``bh rig ready`` (or
      ``None`` when the entry lacks what the probe needs).
    """

    name: str
    cli: typer.Typer
    enabled: Callable[[Any, Any], bool]
    on_onboard: Callable[[Any], None] | None = None
    on_retire: Callable[[Path | str, Any, Any], None] | None = None
    readiness: Callable[[Any, Any], tuple[str, str] | None] | None = None


def registry() -> list[Plugin]:
    """The static list of registered plugins.

    Import-safe: orca is imported lazily inside this function (orca imports ``plugins`` for the
    ``Plugin`` type, so a module-level import here would cycle). New integrations join the list.
    """
    from . import orca  # lazy: avoid the plugins <-> orca import cycle

    return [orca.PLUGIN]
