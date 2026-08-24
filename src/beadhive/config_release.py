"""Typed release-order and Claude plugin policy accessors."""

from __future__ import annotations

import json
from pathlib import Path

from .config_binding import FacadeBinding

_config = FacadeBinding(f"{__package__}.config")


def bind(api) -> None:
    _config.bind(api)


def load():
    return _config.load()


def layered(cfg, entry, section, key, default=None):
    return _config.layered(cfg, entry, section, key, default)


def release_cfg(cfg=None) -> dict:
    """The global `release` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("release", {}) or {}


def release_value(cfg, entry, key: str, default=None):
    """A release setting: per-hive `entry['release'][key]` > global `release[key]` > default."""
    return layered(cfg, entry, "release", key, default)


def release_strategy(cfg, entry) -> str:
    """Named release strategy the scorer registry resolves (default stable-versioning)."""
    return str(release_value(cfg, entry, "strategy", "stable-versioning"))


def release_enforce_hold(cfg, entry) -> bool:
    """Whether a release:breaking bead gets a hard-blocking `release-hold:` gate filed at
    planning time, rather than advisory ordering only (default false)."""
    return bool(release_value(cfg, entry, "enforce_hold", False))


def release_fix_churn_budget(cfg, entry) -> int:
    """Max release:fix beads flushed ahead of features in the current patch window before
    further fixes yield to additive work (default 3)."""
    return int(release_value(cfg, entry, "fix_churn_budget", 3))


def release_conflict_estimator(cfg, entry) -> str:
    """Named ConflictEstimator the start-verdict path consults (default file-overlap, the
    bundled floor implementation)."""
    return str(release_value(cfg, entry, "conflict_estimator", "file-overlap"))


# ---- release channel staleness (bh-7daa6.6) ---------------------------------
# How long `stable` may trail `latest` before `bh doctor` says so. Per-hive-overridable like the
# rest of `release.*`, because the right number is a function of the hive's own release cadence.
# REPORTING ONLY: doctor always exits 0, so no value of either knob can gate a merge or a release
# — a lagging `stable` is the normal state during a soak, which is what the channel is FOR.


def release_channel_stale_days(cfg, entry) -> int:
    """Days the OLDEST unpromoted release may sit before `stable` is called stale. Default **14**;
    ``0`` disables the age check.

    **Why 14, measured rather than picked.** Over beadhive's own `v0.1.0..v0.8.4` — 22 releases
    across 26.1 days — the gap between consecutive releases was: median **0.56 d**, mean 1.24 d,
    p90 2.55 d, **max 9.68 d**. Any age threshold below that observed maximum fires on a repo where
    nothing is wrong (nobody had anything to promote yet), and a warning that fires when nothing is
    wrong is one operators mute. 14 is the smallest round number strictly above the observed
    maximum, with headroom for a quiet fortnight.

    **Why the age threshold is the one that carries the default.** It degrades correctly as cadence
    changes: a slower cadence produces *fewer* unpromoted releases, so the clock simply starts
    later. A count threshold has no such property (see ``release_channel_stale_releases``).

    Reproduce the measurement with::

        git for-each-ref --sort=creatordate --format='%(creatordate:unix)' 'refs/tags/v*'
    """
    return int(release_value(cfg, entry, "channel_stale_days", 14))


def release_channel_stale_releases(cfg, entry) -> int:
    """Releases `stable` may trail `latest` by before being called stale. Default **0 = off**.

    **Why the count check ships disabled.** At beadhive's measured cadence it carries no
    information: `v0.8.1 → v0.8.4` is three releases in **0.1 days**, so a "3 releases behind"
    warning would fire two and a half hours into an ordinary patch burst, every burst. "More than N
    releases behind" is meaningless without knowing cadence, and at this cadence the honest value
    of N is "don't". It stays configurable because a project releasing monthly is in the opposite
    situation — there, three releases behind is a quarter of neglect and the age clock is the blunt
    one. Set it to a positive integer to enable; it ORs with the age check, never replaces it.
    """
    return int(release_value(cfg, entry, "channel_stale_releases", 0))


# ---- claude Code plugin distribution (ws.claude) ----------------------------
# Controls how `ws hive init --claude` installs AGF seat agents + role skills:
#   source=plugin (default) — install the bh Claude Code plugin via the marketplace;
#     agents and skills come from the plugin, nothing is written to .claude/agents/ or ./skills/
#   source=copy (legacy) — copy agents to .claude/agents/ and skills to ./skills/ (old behaviour)
#
# Precedence: per-hive entry['claude'][key] > global claude[key] > built-in default.


def claude_cfg(cfg=None) -> dict:
    """The global `claude` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("claude", {}) or {}


def claude_value(cfg, entry, key: str, default=None):
    """A claude setting: per-hive `entry['claude'][key]` > global `claude[key]` > default."""
    return layered(cfg, entry, "claude", key, default)


def claude_source(cfg=None, entry=None) -> str:
    """Distribution strategy for seat agents + role skills.

    ``plugin`` (default) — install the ``bh`` Claude Code plugin via the configured
    marketplace; nothing is written to ``.claude/agents/`` or ``./skills/``.
    ``copy`` (legacy) — copy agents + skills into the hive as tracked files (old behaviour).
    Unknown values fall back to ``plugin``."""
    val = str(claude_value(cfg, entry, "source", "plugin"))
    return val if val in ("plugin", "copy") else "plugin"


def claude_scope(cfg=None, entry=None) -> str:
    """Install scope for the bh plugin: ``user`` (default) or ``project``."""
    val = str(claude_value(cfg, entry, "scope", "user"))
    return val if val in ("user", "project") else "user"


def _manifest_lists_plugin(manifest: Path, plugin: str) -> bool:
    """True when a marketplace manifest exists and vends ``plugin``."""
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any((p or {}).get("name") == plugin for p in data.get("plugins") or [])


# Canonical remote marketplace (owner/repo form the Claude CLI fetches itself) — the
# fallback when no local clone vends the plugin (e.g. a uv tool / wheel install).
REMOTE_MARKETPLACE = "beadhive/claude-plugin"


def _marketplace_root(cfg, plugin: str) -> Path | None:
    """Anchor for local marketplace values: the PRIMARY CLONE of the registered hive
    whose marketplace manifest vends ``plugin``.

    Anchoring at ``Path(__file__)`` (the running package) is wrong whenever the dev
    CLI runs from an ephemeral bead worktree — it registers the user-level marketplace
    at a path that is reclaimed after merge (dangling marketplace,) —
    and lands in site-packages for wheel installs, where no marketplace exists. The
    registry knows the durable location: hives live at $GIT_WORKSPACE/provider/org/repo,
    so scan ``managed_repos`` for the hive hosting the plugin's marketplace. The package
    anchor survives only when it REALLY hosts a marketplace manifest vending ``plugin``
    (a genuine src checkout) — under a wheel / uv tool install parents[2] is the
    interpreter lib dir where no manifest can exist, so return None and let the caller
    fall back to the canonical remote form."""
    try:
        cfg = cfg if cfg is not None else load()
    except FileNotFoundError:
        cfg = {}
    ws_root = Path(_config._identity_module().workspace_root())
    for e in cfg.get("managed_repos", []) or []:
        root = ws_root / str(e.get("provider", "")) / str(e.get("org", "")) / str(e.get("repo", ""))
        if _manifest_lists_plugin(root / ".claude-plugin" / "marketplace.json", plugin):
            return root
    anchor = Path(_config.__file__).resolve().parents[2]  # package anchor (src checkout only)
    if _manifest_lists_plugin(anchor / ".claude-plugin" / "marketplace.json", plugin):
        return anchor
    return None  # no local marketplace anywhere — caller falls back to the remote form


def claude_marketplace(cfg=None, entry=None) -> str:
    """Marketplace path/identifier for the bh plugin.

    Remote forms (owner/repo, https://…) pass through untouched — the Claude CLI
    fetches them itself. Local values (``.``/``./…``/``/…``/``~/…``) resolve to an
    absolute path: explicit absolute values resolve directly; relative values anchor
    at the registered hive's primary clone (see ``_marketplace_root``) because the
    current Claude CLI rejects a bare ``.``, a relative path would register the
    invoker's cwd, and the running package may live in an ephemeral worktree or in
    site-packages. When no local clone vends the plugin (every field install), the
    default resolves to the canonical remote form ``REMOTE_MARKETPLACE``."""
    val = str(claude_value(cfg, entry, "marketplace", "."))
    if not val.startswith((".", "/", "~")):
        return val  # remote form (owner/repo, https://…) — pass through
    local = Path(val).expanduser()
    if local.is_absolute():
        return str(local.resolve())  # explicit absolute path — no anchor needed
    root = _marketplace_root(cfg, claude_plugin_name(cfg, entry))
    if root is None:
        return REMOTE_MARKETPLACE  # no local marketplace to anchor at — remote fallback
    return str((root / local).resolve())


def claude_plugin_name(cfg=None, entry=None) -> str:
    """Name of the Claude Code plugin that vends Beadflow seat agents. Default ``bh``."""
    return str(claude_value(cfg, entry, "plugin", "bh"))


__all__ = [
    "release_cfg",
    "release_value",
    "release_strategy",
    "release_enforce_hold",
    "release_fix_churn_budget",
    "release_conflict_estimator",
    "release_channel_stale_days",
    "release_channel_stale_releases",
    "claude_cfg",
    "claude_value",
    "claude_source",
    "claude_scope",
    "_manifest_lists_plugin",
    "REMOTE_MARKETPLACE",
    "_marketplace_root",
    "claude_marketplace",
    "claude_plugin_name",
]
