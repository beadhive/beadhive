"""Executable safety contract for Pants' host-wide immutable cache topology."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pants_cache_script", ROOT / "scripts/pants_cache.py")
assert SPEC is not None and SPEC.loader is not None
pants_cache = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pants_cache
SPEC.loader.exec_module(pants_cache)


def _layout(tmp_path: Path, name: str = "one"):
    repository = tmp_path / "worktrees" / name
    repository.mkdir(parents=True)
    return pants_cache.cache_layout(
        repository,
        {"BH_PANTS_CACHE_ROOT": str(tmp_path / "host-cache")},
    )


def test_worktrees_share_only_the_immutable_store(tmp_path) -> None:
    first = _layout(tmp_path, "first")
    second = _layout(tmp_path, "second")

    assert first.local_store == second.local_store
    assert first.maintenance_lock == second.maintenance_lock
    assert first.worktree_root != second.worktree_root
    for key in (
        "PANTS_NAMED_CACHES_DIR",
        "PANTS_WORKDIR",
        "PANTS_SUBPROCESSDIR",
        "UV_CACHE_DIR",
        "PEX_ROOT",
    ):
        assert first.environment()[key] != second.environment()[key]


def test_prepare_enforces_owner_modes_and_capacity_reserves(tmp_path) -> None:
    layout = _layout(tmp_path)
    observed = pants_cache.prepare(layout, min_free_bytes=0, min_free_inodes=0)

    assert observed["free_bytes"] > 0
    assert layout.local_store.stat().st_uid == os.getuid()
    assert layout.local_store.stat().st_mode & 0o777 == 0o775
    assert layout.worktree_root.stat().st_mode & 0o777 == 0o700

    with pytest.raises(pants_cache.CacheSafetyError, match="free bytes"):
        pants_cache.prepare(layout, min_free_bytes=observed["total_bytes"] + 1)
    with pytest.raises(pants_cache.CacheSafetyError, match="free inodes"):
        pants_cache.prepare(layout, min_free_bytes=0, min_free_inodes=observed["total_inodes"] + 1)


def test_active_process_blocks_cleanup_and_exact_recovery(tmp_path, monkeypatch) -> None:
    layout = _layout(tmp_path)
    pants_cache.prepare(layout, min_free_bytes=0, min_free_inodes=0)
    monkeypatch.setenv("BH_PANTS_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("BH_PANTS_MIN_FREE_INODES", "0")
    marker = tmp_path / "active"
    child = (
        "from pathlib import Path; import time; "
        f"Path({str(marker)!r}).write_text('active'); time.sleep(30)"
    )
    command = [sys.executable, "-c", child]
    runner = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/pants_cache.py"),
            "--repository",
            str(layout.repository),
            "run",
            "--",
            *command,
        ],
        env={
            **os.environ,
            "BH_PANTS_CACHE_ROOT": str(layout.cache_root),
            "BH_PANTS_MIN_FREE_BYTES": "0",
            "BH_PANTS_MIN_FREE_INODES": "0",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        with pytest.raises(pants_cache.CacheActiveError):
            pants_cache.cleanup_stale_worktrees(layout, 0)
        with pytest.raises(pants_cache.CacheActiveError):
            pants_cache.reset_worktree_cache(layout, "uv")
    finally:
        runner.terminate()
        runner.wait(timeout=5)


def test_recovery_removes_only_an_exact_entry_and_refuses_broad_paths(tmp_path) -> None:
    layout = _layout(tmp_path)
    pants_cache.prepare(layout, min_free_bytes=0, min_free_inodes=0)
    exact = layout.local_store / "files" / "a" / "abc123"
    exact.parent.mkdir(parents=True)
    exact.write_text("corrupt")
    sibling = exact.with_name("keep")
    sibling.write_text("sound")

    assert pants_cache.recover_local_entry(layout, "files/a/abc123") == exact
    assert not exact.exists()
    assert sibling.read_text() == "sound"
    for unsafe in (".", "files", "../outside", "/tmp/outside"):
        with pytest.raises(pants_cache.CacheSafetyError):
            pants_cache.recover_local_entry(layout, unsafe)


def test_mutable_reset_is_scoped_to_one_worktree_cache(tmp_path) -> None:
    first = _layout(tmp_path, "first")
    second = _layout(tmp_path, "second")
    pants_cache.prepare(first, min_free_bytes=0, min_free_inodes=0)
    pants_cache.prepare(second, min_free_bytes=0, min_free_inodes=0)
    (first.uv_cache / "corrupt").write_text("x")
    (first.named_caches / "keep").write_text("x")
    (second.uv_cache / "keep").write_text("x")

    assert pants_cache.reset_worktree_cache(first, "uv") == first.uv_cache
    assert list(first.uv_cache.iterdir()) == []
    assert (first.named_caches / "keep").exists()
    assert (second.uv_cache / "keep").exists()


def test_failed_command_stays_red_and_names_native_fallback(tmp_path, monkeypatch, capsys) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setenv("BH_PANTS_MIN_FREE_BYTES", "0")
    monkeypatch.setenv("BH_PANTS_MIN_FREE_INODES", "0")

    result = pants_cache.run_pants(layout, [sys.executable, "-c", "raise SystemExit(17)"])

    assert result == 17
    assert "failed closed" in capsys.readouterr().err
    assert pants_cache.FALLBACK == "just check"


def test_status_is_machine_readable_and_remote_defaults_stay_off(tmp_path) -> None:
    layout = _layout(tmp_path)
    report = json.loads(json.dumps(pants_cache.status(layout)))
    config = (ROOT / "pants.toml").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "PANTS.md").read_text(encoding="utf-8")

    assert report["environment"]["PANTS_LOCAL_STORE_DIR"] == str(layout.local_store)
    assert report["fallback"] == "just check"
    assert "remote_cache_read = false" in config
    assert "remote_cache_write = false" in config
    for option in (
        "PANTS_REMOTE_PROVIDER",
        "PANTS_REMOTE_STORE_ADDRESS",
        "PANTS_REMOTE_INSTANCE_NAME",
        "PANTS_REMOTE_CA_CERTS_PATH",
        "PANTS_REMOTE_OAUTH_BEARER_TOKEN",
    ):
        assert option in docs
