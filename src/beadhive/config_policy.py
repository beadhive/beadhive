"""Config migrations and operator-facing policy warnings."""

from __future__ import annotations

from collections.abc import MutableMapping

HIVE_KEY_MIGRATIONS = (
    ("otel", "rig", "hive"),
    ("git_workspace", "rig_match", "hive_match"),
)
LEGACY_KEY_REMOVALS = (("git_workspace", "enabled"),)


def migrate_hive_keys_if_needed(api) -> None:
    try:
        cfg = api.load_host()
    except FileNotFoundError:
        return
    migrated = []
    for section, old_key, new_key in HIVE_KEY_MIGRATIONS:
        section_cfg = cfg.get(section)
        if not isinstance(section_cfg, MutableMapping) or old_key not in section_cfg:
            continue
        if new_key not in section_cfg:
            section_cfg[new_key] = section_cfg[old_key]
        del section_cfg[old_key]
        migrated.append(f"{section}.{old_key} -> {section}.{new_key}")
    for section, old_key in LEGACY_KEY_REMOVALS:
        section_cfg = cfg.get(section)
        if not isinstance(section_cfg, MutableMapping) or old_key not in section_cfg:
            continue
        del section_cfg[old_key]
        migrated.append(f"{section}.{old_key} -> (removed)")
    if not migrated:
        return
    api.save(cfg)
    api._warning("hive_config_keys_migrated", logger_name=api.__name__, migrated=migrated)


def warn_stale_schema_version_if_needed(api) -> None:
    try:
        cfg = api.load()
    except FileNotFoundError:
        return
    from .config_schema import SCHEMA_VERSION

    found = cfg.get("schema_version")
    if isinstance(found, int) and found >= SCHEMA_VERSION:
        return
    api._warning(
        "config_schema_version_stale",
        logger_name=api.__name__,
        found=found,
        current=SCHEMA_VERSION,
        hint=f"run `{api.BINARY_ALIAS} config validate` to check your config",
    )


def hq_has_remote(api) -> bool:
    try:
        return '[remote "' in (api.hq_dir() / ".git" / "config").read_text()
    except Exception:
        return False


def warn_missing_fleet_config_if_needed(api) -> None:
    if not api.hq_dir().is_dir() or api.fleet_path().is_file() or not api._hq_has_remote():
        return
    api._warning(
        "fleet_config_missing",
        logger_name=api.__name__,
        expected=str(api.fleet_path()),
        hint="host-only config in effect until the HQ store provides a fleet.yaml",
    )
