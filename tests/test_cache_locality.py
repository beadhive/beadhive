"""Filesystem and adapter contract for shared framework cache placement."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from beadhive import cache_locality


def _env(tmp_path: Path, **values: str) -> dict[str, str]:
    return {
        "BH_TMPFS_CACHE_DIR": str(tmp_path / "tmp-cache"),
        "BH_DURABLE_CACHE_DIR": str(tmp_path / "durable-cache"),
        "BH_CACHE_MIN_FREE_BYTES": "0",
        "BH_CACHE_MIN_FREE_INODES": "0",
        "XDG_CACHE_HOME": str(tmp_path / "native"),
        **values,
    }


def test_default_and_override_ephemeral_roots_are_app_scoped(tmp_path, monkeypatch) -> None:
    checkout = Path("/tmp/bh-worktrees/example/repository")
    target = checkout / ".venv"
    monkeypatch.setattr(cache_locality, "_device", lambda _path: 42)
    monkeypatch.setattr(cache_locality, "_capacity", lambda _path: (10**9, 10**6))
    monkeypatch.setattr(cache_locality, "_prepare_managed", lambda root, app: root / app)

    default = cache_locality.resolve_cache(
        cache_locality.UV_ADAPTER,
        checkout,
        target,
        {"BH_CACHE_MIN_FREE_BYTES": "0", "BH_CACHE_MIN_FREE_INODES": "0"},
    )
    override = cache_locality.resolve_cache(
        cache_locality.PNPM_ADAPTER,
        checkout,
        checkout / "node_modules",
        {
            "BH_TMPFS_CACHE_DIR": str(tmp_path / "fast"),
            "BH_CACHE_MIN_FREE_BYTES": "0",
            "BH_CACHE_MIN_FREE_INODES": "0",
        },
    )

    assert default.path == Path("/tmp/bh-cache/uv")
    assert override.path == tmp_path / "fast" / "pnpm"
    assert default.environment == {"UV_CACHE_DIR": str(default.path), "UV_LINK_MODE": "hardlink"}
    assert override.environment["PNPM_CONFIG_STORE_DIR"] == str(override.path)
    assert override.environment["PNPM_CONFIG_PACKAGE_IMPORT_METHOD"] == "hardlink"


def test_custom_managed_root_uses_actual_filesystem_classification(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "custom-managed-root"
    checkout = custom / "provider" / "org" / "repo" / "seat"
    checkout.mkdir(parents=True)
    monkeypatch.setattr(cache_locality, "_filesystem_type", lambda path: "tmpfs")

    selected = cache_locality.resolve_cache(
        cache_locality.UV_ADAPTER,
        checkout,
        environ=_env(tmp_path),
        worktree_root=custom,
    )

    assert selected.tier == "ephemeral"


def test_authoritative_persistent_class_wins_even_under_tmp(tmp_path) -> None:
    checkout = tmp_path / "configured-persistent" / "seat"
    checkout.mkdir(parents=True)

    result = cache_locality.command_environment(
        ["uv", "sync"],
        checkout,
        _env(tmp_path),
        worktree_root=tmp_path / "configured-persistent",
        ephemeral=False,
    )

    assert result is not None
    assert result[1].tier == "durable"


def test_configured_ephemeral_uses_authoritative_managed_root_class(tmp_path) -> None:
    root = tmp_path / "configured-root"
    checkout = root / "provider" / "org" / "repo" / "seat"
    checkout.mkdir(parents=True)
    cfg = object()
    configured = SimpleNamespace(
        load=lambda: cfg,
        worktrees_root=lambda value: root,
        worktrees_ephemeral=lambda value: False,
    )
    with patch.object(cache_locality, "config", configured):
        assert cache_locality.configured_ephemeral(checkout) is False
        assert cache_locality.configured_ephemeral(tmp_path / "unmanaged") is False


def test_framework_neutral_adapter_needs_no_core_registration(tmp_path) -> None:
    adapter = cache_locality.CacheAdapter(
        application="synthetic",
        cache_environment="SYNTHETIC_CACHE",
        hardlink_environment=(("SYNTHETIC_LINK", "link"),),
        copy_environment=(("SYNTHETIC_LINK", "copy"),),
        dependency_directory="deps",
    )
    checkout = tmp_path / "persistent" / "checkout"
    checkout.mkdir(parents=True)

    selected = cache_locality.resolve_cache(
        adapter, checkout, environ=_env(tmp_path), ephemeral=False
    )

    assert selected.application == "synthetic"
    assert selected.environment["SYNTHETIC_LINK"] == "link"
    assert selected.path.name == "synthetic"


@pytest.mark.parametrize("application", ["../uv", "a/b", "UV", "", ".hidden"])
def test_application_names_cannot_traverse_the_managed_root(tmp_path, application) -> None:
    adapter = cache_locality.CacheAdapter(application, "CACHE")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with pytest.raises(cache_locality.CacheLocalityError, match="normalized lower-case"):
        cache_locality.resolve_cache(adapter, checkout, environ=_env(tmp_path), ephemeral=True)


def test_explicit_cross_device_uv_override_is_preserved_with_visible_copy_mode(
    tmp_path, monkeypatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    explicit = tmp_path / "other-device" / "uv"

    def devices(path: Path) -> int:
        return 8 if str(path).startswith(str(explicit)) else 7

    monkeypatch.setattr(cache_locality, "_device", devices)
    monkeypatch.setattr(cache_locality, "_capacity", lambda _path: (123, 456))
    selected = cache_locality.resolve_cache(
        cache_locality.UV_ADAPTER,
        checkout,
        environ={"UV_CACHE_DIR": str(explicit)},
        ephemeral=False,
    )

    assert selected.path == explicit
    assert selected.tier == "native-explicit"
    assert selected.environment["UV_LINK_MODE"] == "copy"
    assert "different devices" in selected.diagnostic


def test_explicit_legacy_pnpm_override_uses_current_controls_and_copy_mode(
    tmp_path, monkeypatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    explicit = tmp_path / "other-device" / "pnpm"

    def devices(path: Path) -> int:
        return 8 if str(path).startswith(str(explicit)) else 7

    monkeypatch.setattr(cache_locality, "_device", devices)
    monkeypatch.setattr(cache_locality, "_capacity", lambda _path: (123, 456))
    selected = cache_locality.resolve_cache(
        cache_locality.PNPM_ADAPTER,
        checkout,
        environ=_env(tmp_path, npm_config_store_dir=str(explicit)),
        ephemeral=False,
    )

    assert selected.path == explicit
    assert selected.tier == "native-explicit"
    assert selected.environment["PNPM_CONFIG_STORE_DIR"] == str(explicit)
    assert selected.environment["npm_config_store_dir"] == str(explicit)
    assert selected.environment["PNPM_CONFIG_PACKAGE_IMPORT_METHOD"] == "copy"
    assert selected.environment["npm_config_package_import_method"] == "copy"
    assert "different devices" in selected.diagnostic


def test_unsafe_explicit_override_falls_back_to_safe_durable_cache(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    real = tmp_path / "real-cache"
    real.mkdir()
    linked = tmp_path / "linked-cache"
    linked.symlink_to(real, target_is_directory=True)

    selected = cache_locality.resolve_cache(
        cache_locality.UV_ADAPTER,
        checkout,
        environ=_env(tmp_path, UV_CACHE_DIR=str(linked)),
        ephemeral=False,
    )

    assert selected.tier == "durable"
    assert selected.path == tmp_path / "durable-cache" / "uv"
    assert "explicit native cache unsafe" in selected.diagnostic
    assert "symlink" in selected.diagnostic


def test_low_space_or_cross_device_ephemeral_cache_falls_back_visibly(
    tmp_path, monkeypatch
) -> None:
    checkout = Path("/tmp/bh-worktrees/example/repository")
    env = _env(tmp_path, BH_CACHE_MIN_FREE_BYTES="100")
    monkeypatch.setattr(cache_locality, "_capacity", lambda _path: (50, 50))
    monkeypatch.setattr(cache_locality, "_device", lambda _path: 1)

    selected = cache_locality.resolve_cache(cache_locality.UV_ADAPTER, checkout, environ=env)

    assert selected.tier == "durable"
    assert selected.link_method == "hardlink"
    assert "reserve failed" in selected.diagnostic


def test_safe_directory_rejects_symlink_and_concurrent_initialization_is_idempotent(
    tmp_path,
) -> None:
    root = tmp_path / "shared"
    target = tmp_path / "target"
    target.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: cache_locality._prepare_managed(root, "uv"), range(32)))
    assert set(paths) == {root / "uv"}
    assert (root / "uv").stat().st_uid == os.getuid()
    assert stat_mode(root / "uv") == 0o700

    bad = tmp_path / "bad"
    bad.symlink_to(root, target_is_directory=True)
    with pytest.raises(cache_locality.CacheLocalityError, match="symlink"):
        cache_locality._prepare_managed(bad, "uv")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_command_adapter_exports_uv_and_pnpm_controls(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = _env(tmp_path)

    uv = cache_locality.command_environment(["uv", "sync"], checkout, env)
    pnpm = cache_locality.command_environment(["sh", "-c", "pnpm install"], checkout, env)
    unrelated = cache_locality.command_environment(["git", "status"], checkout, env)

    assert uv is not None and uv[0]["UV_LINK_MODE"] == "hardlink"
    assert pnpm is not None
    assert pnpm[0]["PNPM_CONFIG_STORE_DIR"] == str(pnpm[1].path)
    assert pnpm[0]["PNPM_CONFIG_PACKAGE_IMPORT_METHOD"] == "hardlink"
    assert pnpm[0]["npm_config_store_dir"] == str(pnpm[1].path)
    assert unrelated is None


def test_unsupported_framework_has_visible_copy_fallback_and_cannot_be_pruned(tmp_path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = _env(tmp_path)

    with pytest.raises(cache_locality.CacheLocalityError, match="unsupported"):
        cache_locality.adapter_for_application("cargo")
    assert cache_locality.adapter_for_command(["cargo", "build"]) is None
    selected = cache_locality.unsupported_cache_fallback("cargo", checkout, environ=env)

    assert selected.tier == "unsupported-copy"
    assert selected.link_method == "copy"
    assert selected.environment == {}
    assert "no cache controls exported" in selected.diagnostic
    with pytest.raises(cache_locality.CacheLocalityError, match="does not prune"):
        cache_locality.prune_cache(selected)
