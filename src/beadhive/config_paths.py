"""Machine paths, environment resolution, and packaged config assets.

The public surface remains :mod:`beadhive.config`.  Functions here accept that
facade as a collaborator so its long-standing monkeypatch seams remain runtime
lookups rather than import-time aliases.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BH_", extra="ignore", env_ignore_empty=True)

    home: str | None = Field(None, validation_alias=AliasChoices("BH_HOME", "WS_HOME"))
    config: str | None = Field(None, validation_alias=AliasChoices("BH_CONFIG", "WS_CONFIG"))
    hub: str | None = Field(None, validation_alias=AliasChoices("BH_HUB", "WS_HUB"))
    hq: str | None = Field(None, validation_alias=AliasChoices("BH_HQ", "WS_HQ"))
    cache: str | None = Field(None, validation_alias=AliasChoices("BH_CACHE", "WS_CACHE"))
    worktrees: str | None = Field(
        None, validation_alias=AliasChoices("BH_WORKTREES", "WS_WORKTREES")
    )
    debug: str | None = Field(None, validation_alias=AliasChoices("BH_DEBUG", "WS_DEBUG"))
    bd_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_BD_PASS_ENABLED", "WS_BD_PASS_ENABLED")
    )
    git_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_GIT_PASS_ENABLED", "WS_GIT_PASS_ENABLED")
    )
    skip_setup_check: str | None = Field(
        None, validation_alias=AliasChoices("BH_SKIP_SETUP_CHECK", "WS_SKIP_SETUP_CHECK")
    )
    image_manifest: str | None = Field(None, validation_alias=AliasChoices("BH_IMAGE_MANIFEST"))
    plugin_dir: str | None = Field(None, validation_alias=AliasChoices("BH_PLUGIN_DIR"))
    opencode_skills_home: str | None = Field(
        None, validation_alias=AliasChoices("BH_OPENCODE_SKILLS_HOME")
    )
    claude_home: str | None = Field(None, validation_alias=AliasChoices("BH_CLAUDE_HOME"))
    codex_home: str | None = Field(None, validation_alias=AliasChoices("BH_CODEX_HOME"))
    harness: str | None = Field(None, validation_alias=AliasChoices("BH_HARNESS"))
    role: str | None = Field(None, validation_alias=AliasChoices("BH_ROLE", "WS_ROLE"))
    dev: str | None = Field(None, validation_alias=AliasChoices("BH_DEV", "WS_DEV"))
    crew: str | None = Field(None, validation_alias=AliasChoices("BH_CREW", "WS_CREW"))
    genai_model: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_MODEL", "WS_GENAI_MODEL")
    )
    genai_system: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_SYSTEM", "WS_GENAI_SYSTEM")
    )
    observaloop_profile: str | None = Field(
        None, validation_alias=AliasChoices("BH_OBSERVALOOP_PROFILE", "WS_OBSERVALOOP_PROFILE")
    )


def env(api, field: str) -> str | None:
    value = getattr(api._Env(), field)
    if value is not None:
        aliases = api._Env.model_fields[field].validation_alias.choices
        if len(aliases) > 1 and os.environ.get(aliases[0]) is None and os.environ.get(aliases[1]):
            api._warning(
                "deprecated_env_var",
                logger_name=api.__name__,
                old=aliases[1],
                new=aliases[0],
                hint=f"set {aliases[0]} instead — {aliases[1]} support will be removed later",
            )
    return value


def home(api) -> Path:
    value = api._env("home")
    return Path(value).expanduser() if value else api._DEFAULT_HOME_NEW


def config_path(api) -> Path:
    value = api._env("config")
    return Path(value).expanduser() if value else api.home() / "config.yaml"


def named_home(api, field: str, default: str) -> Path:
    value = api._env(field)
    return Path(value).expanduser() if value else api.home() / default


def worktrees_root(api, cfg=None) -> Path:
    value = api._env("worktrees")
    if value:
        return Path(value).expanduser()
    if api.worktrees_ephemeral(cfg):
        return Path(tempfile.gettempdir()) / "bh-worktrees"
    path = api.worktrees_cfg(cfg).get("path") or str(api.home() / "worktrees")
    return Path(path).expanduser()


def codex_sandbox_active() -> bool:
    return bool(os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED", "").strip())


def codex_default_sandbox_covers(path: Path) -> bool:
    try:
        target = path.resolve()
    except OSError:
        return True
    return any(
        target == root or target.is_relative_to(root)
        for root in (Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve())
    )


def package_asset(package: str, *parts: str) -> Path:
    target = files(package)
    for part in parts:
        target = target / part
    return Path(str(target))


def scaffold_home(api, force: bool = False, dry_run: bool = False) -> list[tuple[Path, bool]]:
    host = api._host_module()
    pairs = [
        (api.template("config.example.yaml"), api.config_path()),
        (api.template("docker-compose.yml"), api.compose_file()),
        (api.template("docker-compose.otel.yml"), api.otel_compose_file()),
        (api.template("env.example"), api.home() / ".env.example"),
    ]
    if not dry_run:
        api.home().mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, bool]] = []
    for src, dst in pairs:
        if dst.exists() and not force:
            results.append((dst, False))
        elif dry_run:
            results.append((dst, True))
        else:
            shutil.copy(src, dst)
            results.append((dst, True))
    results.append((host.path(), (not host.path().exists()) if dry_run else host.mint_if_needed()))
    return results


def plugin_root(api, cfg=None) -> Path:
    override = api._Env().plugin_dir
    if override:
        return Path(override).expanduser()
    try:
        cfg = cfg if cfg is not None else api.load()
    except FileNotFoundError:
        cfg = {}
    plugin = api.claude_plugin_name(cfg)
    root = api._marketplace_root(cfg, plugin) or Path(api.__file__).resolve().parents[2]
    manifest = root / ".claude-plugin" / "marketplace.json"
    try:
        for item in json.loads(manifest.read_text()).get("plugins") or []:
            if (item or {}).get("name") == plugin:
                return (root / str(item.get("source") or ".")).resolve()
    except (OSError, json.JSONDecodeError):
        pass
    return root


def harness_home(api, field: str, *default: str) -> Path:
    override = getattr(api._Env(), field)
    return Path(override).expanduser() if override else Path.home().joinpath(*default)
