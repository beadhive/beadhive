"""Compatibility collaborators for canonical migration and warning policy."""

from __future__ import annotations

from .modules.config.application.migrations import ConfigMigrationService
from .modules.config.application.resolution import ConfigResolutionError
from .modules.config.domain.ports import ConfigScope

# Historical data shapes remain visible on the facade while canonical policy uses KeyMigration.
HIVE_KEY_MIGRATIONS = (
    ("otel", "rig", "hive"),
    ("git_workspace", "rig_match", "hive_match"),
)
LEGACY_KEY_REMOVALS = (("git_workspace", "enabled"),)


class _FacadeMigrationStore:
    def __init__(self, api) -> None:
        self._api = api

    def load_document(self, scope: ConfigScope, *, missing_ok: bool = False):
        del missing_ok
        if scope != ConfigScope.HOST:
            raise ValueError(f"unsupported migration scope: {scope}")
        return self._api.load_host()

    def save_document(self, scope: ConfigScope, document) -> None:
        if scope != ConfigScope.HOST:
            raise ValueError(f"unsupported migration scope: {scope}")
        self._api.save(document)


def migrate_hive_keys_if_needed(api) -> None:
    def report(migrated: tuple[str, ...]) -> None:
        api._warning(
            "hive_config_keys_migrated",
            logger_name=api.__name__,
            migrated=list(migrated),
        )

    try:
        ConfigMigrationService(_FacadeMigrationStore(api), report).migrate_host()
    except ConfigResolutionError as exc:
        raise api.ConfigError(str(exc)) from None


def warn_stale_schema_version_if_needed(api) -> None:
    try:
        cfg = api.load()
    except FileNotFoundError:
        return
    from .modules.config.contracts import SCHEMA_VERSION

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
