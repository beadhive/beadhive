"""Import-compatible composition facade for Beadhive configuration.

Concrete ownership lives in ``config_paths`` (environment/assets/paths),
``config_store`` (host/fleet persistence), ``config_edit`` (dotted mutation),
and the cohesive typed policy modules installed at the bottom of this file.
Facade wrappers deliberately pass this module as a collaborator so historical
monkeypatch seams remain runtime lookups.
"""

from __future__ import annotations

import sys
import tempfile  # noqa: F401  # compatibility: callers patch config.tempfile.gettempdir
from pathlib import Path

from . import config_edit as _config_edit
from . import config_paths as _config_paths
from . import config_policy as _config_policy
from . import config_store as _config_store

BINARY_NAME = "beadhive"
BINARY_ALIAS = "bh"

_Env = _config_paths.Env
_DEFAULT_HOME_OLD = Path("~/.ws").expanduser()
_DEFAULT_HOME_NEW = Path("~/.beadhive").expanduser()
_UNSET = object()

_yaml = _config_store.yaml
_yaml_lock = _config_store.yaml_lock

FLEET_FILE = "fleet.yaml"
SCOPE_FLEET = "fleet"
SCOPE_HOST = "host"
PROVENANCE_FLEET = "fleet"
PROVENANCE_HOST = "host"
PROVENANCE_OVERRIDE = "override"

_HIVE_KEY_MIGRATIONS = _config_policy.HIVE_KEY_MIGRATIONS
_LEGACY_KEY_REMOVALS = _config_policy.LEGACY_KEY_REMOVALS


class ConfigError(ValueError):
    """The host/fleet layers cannot be resolved into one effective view."""


# Schema ownership intentionally remains where bh-1h9h left it. This extraction
# moves mechanics only and neither derives nor expands the known-section policy.
KNOWN_SECTIONS = frozenset(
    {
        "delimiter",
        "providers",
        "orgs",
        "exclude",
        "dimensions",
        "dolt",
        "beads",
        "work",
        "hq",
        "release",
        "managed_repos",
        "log",
        "otel",
        "observaloop",
        "repowise",
        "worktrees",
        "archive",
        "backup",
        "metadata",
        "passthrough",
        "claude",
        "harness",
        "hitch",
    }
)


def _facade():
    return sys.modules[__name__]


def _warning(event: str, *, logger_name: str | None = None, **fields) -> None:
    """Emit a config diagnostic without making implementation modules import ``log``."""
    from . import log

    log.get_logger(logger_name or __name__).warning(event, **fields)


def _host_module():
    from . import host

    return host


def _guard_module():
    from . import guard

    return guard


def _identity_module():
    from . import identity

    return identity


def _seat_runners():
    from . import deps

    return deps.seat_runners()


def layered(cfg, entry, section, key, default=None):
    """Resolve per-hive > global > default for a possibly dotted section."""
    parts = section.split(".")
    hive = entry or {}
    for part in parts:
        hive = (hive or {}).get(part) or {}
    if key in hive:
        return hive[key]
    cfg = cfg if cfg is not None else load()
    glob = cfg or {}
    for part in parts:
        glob = (glob or {}).get(part) or {}
    return glob[key] if key in glob else default


def layered_flag(cfg, entry, section, key="enabled", default=False):
    value = layered(cfg, entry, section, key, _UNSET)
    return default if value is _UNSET else bool(value)


# Paths and packaged assets ---------------------------------------------------


def _env(field: str) -> str | None:
    return _config_paths.env(_facade(), field)


def home() -> Path:
    return _config_paths.home(_facade())


def config_path() -> Path:
    return _config_paths.config_path(_facade())


def hub_dir() -> Path:
    return _config_paths.named_home(_facade(), "hub", "hub")


def hq_dir() -> Path:
    return _config_paths.named_home(_facade(), "hq", "hq")


def cache_dir() -> Path:
    return _config_paths.named_home(_facade(), "cache", "cache")


def worktrees_root(cfg=None) -> Path:
    return _config_paths.worktrees_root(_facade(), cfg)


def codex_sandbox_active() -> bool:
    return _config_paths.codex_sandbox_active()


def codex_default_sandbox_covers(path: Path) -> bool:
    return _config_paths.codex_default_sandbox_covers(path)


def docs_path() -> Path:
    return home() / "labels.md"


def compose_file() -> Path:
    return home() / "docker-compose.yml"


def otel_compose_file() -> Path:
    return home() / "docker-compose.otel.yml"


def env_file() -> Path:
    return home() / ".env"


def asset(name: str) -> Path:
    return _config_paths.package_asset("beadhive.assets", name)


def template(name: str) -> Path:
    return _config_paths.package_asset("beadhive.templates", name)


def scaffold_home(force: bool = False, dry_run: bool = False) -> list[tuple[Path, bool]]:
    return _config_paths.scaffold_home(_facade(), force, dry_run)


def observaloop_dashboard_asset() -> Path:
    return _config_paths.package_asset("beadhive.assets", "observaloop", "bh-dashboard.json")


def observaloop_metrics_preset_asset() -> Path:
    return _config_paths.package_asset("beadhive.assets", "observaloop", "cli-metrics-preset.yaml")


def _plugin_root(cfg=None) -> Path:
    return _config_paths.plugin_root(_facade(), cfg)


def skills_src() -> Path:
    return _plugin_root() / "skills"


def agents_src() -> Path:
    return _plugin_root() / "agents"


def claude_home() -> Path:
    return _config_paths.harness_home(_facade(), "claude_home", ".claude")


def codex_home() -> Path:
    return _config_paths.harness_home(_facade(), "codex_home", ".codex")


def opencode_skills_home() -> Path:
    return _config_paths.harness_home(
        _facade(), "opencode_skills_home", ".config", "opencode", "skills"
    )


# Host/fleet storage ----------------------------------------------------------


def fleet_path() -> Path:
    return hq_dir() / FLEET_FILE


def load_host():
    return _config_store.load_path(_facade(), config_path())


def load_fleet():
    return _config_store.load_path(_facade(), fleet_path(), missing_ok=True)


def _leaf_paths(node, prefix: str = ""):
    yield from _config_store.leaf_paths(node, prefix)


def fleet_override_violations(host) -> list[str]:
    return _config_store.fleet_override_violations(host)


def _deep_merge(base, over):
    return _config_store.deep_merge(base, over)


def _reject_fleet_overrides(host) -> None:
    _config_store.reject_fleet_overrides(_facade(), host)


def _reject_fleet_override_for_key(parts: list[str], value) -> None:
    _config_store.reject_fleet_override_for_key(_facade(), parts, value)


def load():
    return _config_store.load(_facade())


def key_provenance() -> dict[str, str]:
    return _config_store.key_provenance(_facade())


def _guard_hq_registry_controller() -> None:
    _config_store.guard_hq_registry_controller(_facade())


def save(data) -> None:
    _config_store.save_host(_facade(), data)


def save_fleet(data) -> None:
    _config_store.save_fleet(_facade(), data)


def reconcile_host_after_fleet() -> list[str]:
    return _config_store.reconcile_host_after_fleet(_facade())


def load_reconciling() -> dict:
    return _config_store.load_reconciling(_facade())


def _write_transaction(scope: str):
    path = fleet_path() if scope == SCOPE_FLEET else config_path()
    return _config_store.mutation(path)


# Migrations and warnings -----------------------------------------------------


def migrate_hive_keys_if_needed() -> None:
    _config_policy.migrate_hive_keys_if_needed(_facade())


def warn_stale_schema_version_if_needed() -> None:
    _config_policy.warn_stale_schema_version_if_needed(_facade())


def _hq_has_remote() -> bool:
    return _config_policy.hq_has_remote(_facade())


def warn_missing_fleet_config_if_needed() -> None:
    _config_policy.warn_missing_fleet_config_if_needed(_facade())


# Dotted editing --------------------------------------------------------------


def _problem(level: str, message: str) -> dict:
    return _config_edit.problem(level, message)


def _not_set_message(dotted: str) -> str:
    return _config_edit.not_set_message(dotted)


def _has_errors(problems) -> bool:
    return _config_edit.has_errors(problems)


def _split_key(dotted: str) -> list[str]:
    return _config_edit.split_key(dotted)


def coerce_value(raw: str, as_json: bool = False):
    return _config_edit.coerce_value(raw, as_json)


def _validate(parts: list[str], value) -> list[dict]:
    return _config_edit.validate(_facade(), parts, value)


def literal_violations(cfg=None) -> list[dict]:
    return _config_edit.literal_violations(_facade(), cfg)


def warn_literal_violations_if_needed() -> None:
    _config_edit.warn_literal_violations(_facade())


def _descend(cfg, parts: list[str]):
    return _config_edit.descend(cfg, parts)


def get_value(dotted: str, cfg=None, scope: str | None = None) -> dict:
    return _config_edit.get_value(_facade(), dotted, cfg, scope)


def set_value(
    dotted: str, raw: str, as_json: bool = False, cfg=None, scope: str | None = None
) -> dict:
    return _config_edit.set_value(_facade(), dotted, raw, as_json, cfg, scope)


def unset_value(dotted: str, cfg=None, scope: str | None = None) -> dict:
    return _config_edit.unset_value(_facade(), dotted, cfg, scope)


def _delete_leaf_pruning_empty(node: dict, dotted: str) -> None:
    unset_value(dotted, cfg=node)


def set_hive_feature_flag(entry, feature: str, enabled: bool) -> dict:
    return _config_edit.set_hive_feature_flag(_facade(), entry, feature, enabled)


# Typed domain accessors ------------------------------------------------------


def _install_domain_facades() -> None:
    from . import config_release, config_services, config_work_settings

    for module in (config_services, config_work_settings, config_release):
        module.bind(_facade())
        for name in module.__all__:
            globals()[name] = getattr(module, name)


_install_domain_facades()
del _install_domain_facades
