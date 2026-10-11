"""``hive.sync`` resolved per hive (trust-profiles ADR section 5, bh-midl0).

The keys are validated by :class:`beadhive.modules.config.contracts.HiveSyncConfig` (the fleet
default ``hive.sync``) and :class:`~beadhive.modules.config.contracts.HiveSyncOverride` (a hive's
own ``managed_repos[].hive.sync``). This module merges the two key by key into one
:class:`SyncSettings` and answers the three questions the rest of bh asks:

* :func:`for_entry` — what does the host daemon's sync loop do for this hive?
* :func:`durable` — does this verb push synchronously? ``submit`` and ``finish`` always do
  (partitioning ADR condition 18), whatever the configuration says.
* :func:`bd_env` — ``auto_commit`` mapped onto bd's ``dolt.auto-commit``: bh's bd calls in a hive
  set to ``batch`` run with ``BD_DOLT_AUTO_COMMIT=batch`` (bd's own environment key for that
  config value), so no tracked ``.beads/config.yaml`` is rewritten.

Pure: it reads only the configuration mapping it is handed (``bd_env`` loads it once per
process through the daemon's narrow settings port).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .modules.config.contracts import (
    SYNC_REQUIRED_DURABLE_VERBS,
    sync_backoff_seconds,
    sync_duration_seconds,
)

#: bd's environment key for its ``dolt.auto-commit`` config value.
BD_AUTO_COMMIT_ENV = "BD_DOLT_AUTO_COMMIT"

#: The ADR defaults. The intervals are PROVISIONAL until the push-cadence spike bh-infra-b7at
#: (central remote push cost and aggregator staleness) reports a measured cadence.
DEFAULT_AUTO_COMMIT = "on"
DEFAULT_PUSH_INTERVAL = "5m"
DEFAULT_MAX_PENDING = 50
DEFAULT_RETRY_BACKOFF = "30s..10m"
DEFAULT_PULL_INTERVAL = "15m"
CADENCE_EVIDENCE = "bh-infra-b7at"


@dataclass(frozen=True)
class SyncSettings:
    """One hive's effective ``hive.sync``."""

    auto_commit: str = DEFAULT_AUTO_COMMIT
    push_enabled: bool = False
    push_interval_s: float = 300.0
    max_pending: int = DEFAULT_MAX_PENDING
    backoff_first_s: float = 30.0
    backoff_cap_s: float = 600.0
    pull_enabled: bool = False
    pull_interval_s: float = 900.0
    durable_verbs: tuple[str, ...] = SYNC_REQUIRED_DURABLE_VERBS

    @property
    def active(self) -> bool:
        """Whether the daemon has anything to do for this hive."""
        return self.push_enabled or self.pull_enabled

    def backoff_s(self, failures: int) -> float:
        """The delay before the next push after `failures` consecutive failures (>= 1):
        ``first * 2**(failures-1)``, capped."""
        if failures <= 0:
            return 0.0
        return min(self.backoff_cap_s, self.backoff_first_s * (2 ** min(failures - 1, 32)))

    def as_dict(self) -> dict:
        return {
            "auto_commit": self.auto_commit,
            "auto_push": {
                "enabled": self.push_enabled,
                "interval_s": self.push_interval_s,
                "max_pending": self.max_pending,
                "retry_backoff_s": [self.backoff_first_s, self.backoff_cap_s],
            },
            "auto_pull": {"enabled": self.pull_enabled, "interval_s": self.pull_interval_s},
            "durable_verbs": list(self.durable_verbs),
            "cadence_evidence": f"provisional until {CADENCE_EVIDENCE} reports",
        }


def _section(value, name: str) -> Mapping:
    found = value.get(name) if isinstance(value, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def _pick(fleet: Mapping, own: Mapping, key: str, default):
    value = own.get(key)
    if value is None:
        value = fleet.get(key)
    return default if value is None else value


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "yes", "1")
    return bool(value)


def _auto_commit(value) -> str:
    if value is True:
        return "on"
    text = str(value or DEFAULT_AUTO_COMMIT).strip().lower()
    return text if text in ("on", "batch") else DEFAULT_AUTO_COMMIT


def _seconds(value, default: str) -> float:
    try:
        return sync_duration_seconds(value)
    except ValueError:
        return sync_duration_seconds(default)


def _backoff(value) -> tuple[float, float]:
    try:
        return sync_backoff_seconds(value)
    except ValueError:
        return sync_backoff_seconds(DEFAULT_RETRY_BACKOFF)


def _durable(value) -> tuple[str, ...]:
    verbs = [str(verb) for verb in value] if isinstance(value, list | tuple) else []
    for required in reversed(SYNC_REQUIRED_DURABLE_VERBS):
        if required not in verbs:
            verbs.insert(0, required)
    return tuple(verbs)


def resolve(fleet: Mapping | None, own: Mapping | None = None) -> SyncSettings:
    """Merge the fleet default `fleet` (``hive.sync``) with a hive's own `own`
    (``managed_repos[].hive.sync``) key by key. A value that does not validate (a hand-edited
    config that skipped ``bh config`` validation) falls back to its ADR default rather than
    failing an unrelated command."""
    fleet = fleet if isinstance(fleet, Mapping) else {}
    own = own if isinstance(own, Mapping) else {}
    fleet_push, own_push = _section(fleet, "auto_push"), _section(own, "auto_push")
    fleet_pull, own_pull = _section(fleet, "auto_pull"), _section(own, "auto_pull")
    first, cap = _backoff(_pick(fleet_push, own_push, "retry_backoff", DEFAULT_RETRY_BACKOFF))
    try:
        max_pending = max(1, int(_pick(fleet_push, own_push, "max_pending", DEFAULT_MAX_PENDING)))
    except (TypeError, ValueError):
        max_pending = DEFAULT_MAX_PENDING
    return SyncSettings(
        auto_commit=_auto_commit(_pick(fleet, own, "auto_commit", DEFAULT_AUTO_COMMIT)),
        push_enabled=_bool(_pick(fleet_push, own_push, "enabled", False)),
        push_interval_s=_seconds(
            _pick(fleet_push, own_push, "interval", DEFAULT_PUSH_INTERVAL), DEFAULT_PUSH_INTERVAL
        ),
        max_pending=max_pending,
        backoff_first_s=first,
        backoff_cap_s=cap,
        pull_enabled=_bool(_pick(fleet_pull, own_pull, "enabled", False)),
        pull_interval_s=_seconds(
            _pick(fleet_pull, own_pull, "interval", DEFAULT_PULL_INTERVAL), DEFAULT_PULL_INTERVAL
        ),
        durable_verbs=_durable(_pick(fleet, own, "durable_verbs", None)),
    )


def for_entry(entry: Mapping | None, cfg: Mapping | None) -> SyncSettings:
    """The effective ``hive.sync`` for the registered hive `entry` under the merged `cfg`."""
    return resolve(
        _section(_section(cfg, "hive"), "sync"), _section(_section(entry, "hive"), "sync")
    )


def durable(verb: str, settings: SyncSettings) -> bool:
    """Whether `verb` pushes synchronously. ``submit`` and ``finish`` always do."""
    return verb in SYNC_REQUIRED_DURABLE_VERBS or verb in settings.durable_verbs


def configured(cfg: Mapping | None) -> bool:
    """Whether `cfg` sets ``hive.sync`` anywhere (fleet default or any hive): the cheap test
    that keeps every other bd call free of a registry lookup."""
    if _section(_section(cfg, "hive"), "sync"):
        return True
    repos = cfg.get("managed_repos") if isinstance(cfg, Mapping) else None
    return any(_section(_section(entry, "hive"), "sync") for entry in repos or ())


_AUTO_COMMIT_CACHE: dict[str, str] = {}
#: The configuration this process read once for ``bd_env`` (``{}`` when unreadable).
_PROCESS_CFG: list[Mapping] = []


def reset_cache() -> None:
    _AUTO_COMMIT_CACHE.clear()
    _PROCESS_CFG.clear()


def auto_commit_for(hive_dir, cfg: Mapping | None) -> str:
    """``hive.sync.auto_commit`` for the hive checkout at `hive_dir` (``on`` when no hive)."""
    if not configured(cfg):
        return DEFAULT_AUTO_COMMIT
    from . import registry

    try:
        entry = registry.entry_for_dir(cfg, Path(hive_dir))
    except Exception:  # noqa: BLE001 - an unresolvable directory is no hive: bd's default
        return DEFAULT_AUTO_COMMIT
    return for_entry(entry, cfg).auto_commit if entry else DEFAULT_AUTO_COMMIT


def _process_auto_commit(hive_dir) -> str:
    """:func:`auto_commit_for` against the configuration read once per process: one config
    read, and no registry lookup at all unless ``hive.sync`` is set somewhere."""
    if not _PROCESS_CFG:
        try:
            from .config_consumer_ports import daemon_settings as config

            _PROCESS_CFG.append(config.load() or {})
        except Exception:  # noqa: BLE001 - no config: bd's own default everywhere
            _PROCESS_CFG.append({})
    cfg = _PROCESS_CFG[0]
    if not configured(cfg):
        return DEFAULT_AUTO_COMMIT
    key = str(hive_dir)
    if key not in _AUTO_COMMIT_CACHE:
        _AUTO_COMMIT_CACHE[key] = auto_commit_for(hive_dir, cfg)
    return _AUTO_COMMIT_CACHE[key]


def bd_env(hive_dir, env: Mapping | None, *, cfg: Mapping | None = None) -> Mapping | None:
    """`env` for a bd call in `hive_dir`, carrying ``BD_DOLT_AUTO_COMMIT=batch`` when the hive
    runs ``hive.sync.auto_commit: batch``. Unchanged (``None`` stays ``None``) otherwise, and
    whenever the caller or the process already set the key: an explicit choice wins."""
    if hive_dir is None:
        return env
    if BD_AUTO_COMMIT_ENV in (env if env is not None else os.environ):
        return env
    mode = auto_commit_for(hive_dir, cfg) if cfg is not None else _process_auto_commit(hive_dir)
    if mode == DEFAULT_AUTO_COMMIT:
        return env
    merged = dict(os.environ if env is None else env)
    merged[BD_AUTO_COMMIT_ENV] = mode
    return merged
