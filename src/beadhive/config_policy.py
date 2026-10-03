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
        self._original_bytes: bytes | None = None

    def load_document(self, scope: ConfigScope, *, missing_ok: bool = False):
        del missing_ok
        if scope != ConfigScope.HOST:
            raise ValueError(f"unsupported migration scope: {scope}")
        self._original_bytes = self._api.config_path().read_bytes()
        return self._api.load_host_raw_for_repair()

    def save_document(self, scope: ConfigScope, document) -> None:
        if scope != ConfigScope.HOST:
            raise ValueError(f"unsupported migration scope: {scope}")
        if self._original_bytes is None:
            raise ValueError("migration has no original HOST document")
        self._api._save_host_legacy_migration(document, original_bytes=self._original_bytes)


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
    # This is a diagnostic over opaque source, not a usable settings read. An
    # explicit old/invalid version must still be reported when normal load()
    # correctly refuses it; no candidate is published from this path.
    try:
        cfg = api.load_host_raw_for_repair()
    except FileNotFoundError:
        cfg = {}
    if "schema_version" not in cfg:
        try:
            fleet = api.load_fleet_raw_for_repair()
        except FileNotFoundError:
            fleet = {}
        if "schema_version" in fleet:
            cfg = fleet
        elif not cfg and not fleet:
            return
    from .modules.config.contracts import SCHEMA_VERSION

    found = cfg.get("schema_version")
    if type(found) is int and found >= SCHEMA_VERSION:
        return
    api._warning(
        "config_schema_version_stale",
        logger_name=api.__name__,
        found=found if type(found) is int else "missing" if found is None else "invalid",
        current=SCHEMA_VERSION,
        hint=f"run `{api.BINARY_ALIAS} config validate` to check your config",
    )


def hq_has_remote(api) -> bool:
    if api.fleet_sql_selected():
        return False
    try:
        return '[remote "' in (api.hq_dir() / ".git" / "config").read_text()
    except Exception:
        return False


def warn_missing_fleet_config_if_needed(api) -> None:
    if api.fleet_sql_selected():
        return
    if not api.hq_dir().is_dir() or api.fleet_path().is_file() or not api._hq_has_remote():
        return
    api._warning(
        "fleet_config_missing",
        logger_name=api.__name__,
        expected=str(api.fleet_path()),
        hint="host-only config in effect until the HQ store provides a fleet.yaml",
    )
