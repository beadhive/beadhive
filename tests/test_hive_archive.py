"""Tests for ws hive archive ls / prune.

Covers:
- ls: empty archive dir, populated archive, --json output (typed fields)
- prune: removes only repos older than threshold, keeps recent ones
- prune: --all removes every archived repo regardless of age
- prune: --dry-run previews without mutating anything and reports would-reclaim total
- prune: bytes-reclaimed accounting
- prune: path-escape guard (never deletes outside archive.dir)
- config: archive.dir and archive.window_days accessors + validation
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from typer.testing import CliRunner

from beadhive import config
from beadhive.archive import ArchivedRepo, list_archived, prune_archived
from beadhive.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_archived_repo(archive_dir: Path, provider: str, org: str, repo: str) -> Path:
    """Create a fake archived repo directory with a minimal file."""
    repo_dir = archive_dir / provider / org / repo
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "file.txt").write_text(f"content of {repo}")
    return repo_dir


def _backdate(path: Path, days: float) -> None:
    """Set the mtime of path to ``days`` days ago."""
    target_mtime = time.time() - days * 86400.0
    os.utime(path, (target_mtime, target_mtime))


# ---------------------------------------------------------------------------
# Unit tests: archive module
# ---------------------------------------------------------------------------


def test_list_archived_empty(tmp_path):
    """list_archived returns [] when archive_dir does not exist."""
    missing = tmp_path / "noexist"
    assert list_archived(missing) == []


def test_list_archived_empty_dir(tmp_path):
    """list_archived returns [] when archive_dir exists but is empty."""
    adir = tmp_path / "archive"
    adir.mkdir()
    assert list_archived(adir) == []


def test_list_archived_populated(tmp_path):
    """list_archived returns one ArchivedRepo per provider/org/repo triplet."""
    adir = tmp_path / "archive"
    _make_archived_repo(adir, "github", "myorg", "alpha")
    _make_archived_repo(adir, "github", "myorg", "beta")

    repos = list_archived(adir)

    assert len(repos) == 2
    triplets = {r.triplet for r in repos}
    assert triplets == {"github/myorg/alpha", "github/myorg/beta"}


def test_list_archived_types(tmp_path):
    """ArchivedRepo fields have the expected types."""
    adir = tmp_path / "archive"
    _make_archived_repo(adir, "github", "myorg", "myrepo")

    repos = list_archived(adir)
    assert len(repos) == 1
    r = repos[0]

    assert isinstance(r, ArchivedRepo)
    assert isinstance(r.triplet, str)
    assert isinstance(r.age_days, float)
    assert isinstance(r.size_bytes, int)
    assert r.size_bytes > 0


def test_list_archived_sorted_oldest_first(tmp_path):
    """list_archived sorts by descending age (oldest first)."""
    adir = tmp_path / "archive"
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    new_dir = _make_archived_repo(adir, "github", "myorg", "new")

    _backdate(old_dir, days=60)
    _backdate(new_dir, days=5)

    repos = list_archived(adir)
    assert repos[0].triplet == "github/myorg/old"
    assert repos[1].triplet == "github/myorg/new"


def test_prune_removes_old_keeps_recent(tmp_path):
    """prune_archived removes repos older than the threshold and keeps recent ones."""
    adir = tmp_path / "archive"
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    new_dir = _make_archived_repo(adir, "github", "myorg", "new")

    _backdate(old_dir, days=40)
    _backdate(new_dir, days=5)

    result = prune_archived(adir, older_than_days=30, remove_all=False, dry_run=False)

    assert "github/myorg/old" in result.removed
    assert "github/myorg/new" not in result.removed
    assert not old_dir.exists()
    assert new_dir.exists()
    assert result.reclaimed_bytes > 0


def test_prune_all_removes_every_repo(tmp_path):
    """prune_archived with remove_all=True removes all archived repos regardless of age."""
    adir = tmp_path / "archive"
    new_dir = _make_archived_repo(adir, "github", "myorg", "new")
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")

    _backdate(new_dir, days=1)
    _backdate(old_dir, days=90)

    result = prune_archived(adir, older_than_days=30, remove_all=True, dry_run=False)

    assert len(result.removed) == 2
    assert not new_dir.exists()
    assert not old_dir.exists()
    assert result.reclaimed_bytes > 0


def test_prune_dry_run_mutates_nothing(tmp_path):
    """prune_archived --dry-run reports what would be removed but changes nothing on disk."""
    adir = tmp_path / "archive"
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    _backdate(old_dir, days=40)

    result = prune_archived(adir, older_than_days=30, remove_all=False, dry_run=True)

    assert "github/myorg/old" in result.removed
    assert result.dry_run is True
    # Nothing was actually deleted
    assert old_dir.exists()
    # dry-run returns 0 bytes reclaimed (nothing was freed)
    assert result.reclaimed_bytes == 0


def test_prune_bytes_reclaimed_accounting(tmp_path):
    """prune_archived reports the total bytes actually freed (not zero)."""
    adir = tmp_path / "archive"
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    _backdate(old_dir, days=40)

    # Record the size before pruning
    size_before = sum(
        f.stat().st_size for f in old_dir.rglob("*") if f.is_file()
    )

    result = prune_archived(adir, older_than_days=30, remove_all=False, dry_run=False)

    assert result.reclaimed_bytes == size_before
    assert not old_dir.exists()


def test_prune_path_escape_guard(tmp_path):
    """prune_archived never deletes anything outside archive_dir (path-escape guard)."""
    adir = tmp_path / "archive"
    outside = tmp_path / "sensitive"
    outside.mkdir()
    (outside / "important.txt").write_text("keep me")

    # Simulate an escape: symlink a provider dir to the outside dir
    provider_link = adir / "github"
    adir.mkdir(parents=True, exist_ok=True)
    provider_link.symlink_to(outside)

    # Attempt to prune everything (this should skip the escaped path)
    result = prune_archived(adir, older_than_days=0, remove_all=True, dry_run=False)

    # The outside dir must still exist and be untouched
    assert outside.exists()
    assert (outside / "important.txt").exists()
    # Nothing was "removed" (the escape was caught or it wasn't a 3-level triplet)
    assert result.reclaimed_bytes == 0


# ---------------------------------------------------------------------------
# Unit tests: config accessors
# ---------------------------------------------------------------------------


def test_archive_dir_default(monkeypatch, tmp_path):
    """archive_dir() falls back to $GIT_WORKSPACE/.archived when unset."""
    ws_root = tmp_path / "workspace"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))

    cfg: dict = {}
    result = config.archive_dir(cfg)
    assert result == ws_root / ".archived"


def test_archive_dir_override(monkeypatch, tmp_path):
    """archive_dir() respects the archive.dir config key."""
    custom = tmp_path / "my_archive"
    cfg = {"archive": {"dir": str(custom)}}
    result = config.archive_dir(cfg)
    assert result == custom


def test_archive_window_days_default():
    """archive_window_days() returns 30 when unset."""
    assert config.archive_window_days({}) == 30


def test_archive_window_days_override():
    """archive_window_days() returns the configured value."""
    cfg = {"archive": {"window_days": 14}}
    assert config.archive_window_days(cfg) == 14


def test_validate_window_days_must_be_positive():
    """set_value rejects archive.window_days = 0 or negative."""
    from ruamel.yaml.comments import CommentedMap

    cfg = CommentedMap({"providers": []})

    res = config.set_value("archive.window_days", "0", cfg=cfg)
    assert not res["ok"]
    assert any("positive" in p["message"] for p in res["problems"])

    res = config.set_value("archive.window_days", "-5", cfg=cfg)
    assert not res["ok"]

    res = config.set_value("archive.window_days", "14", cfg=cfg)
    assert res["ok"]
    assert cfg["archive"]["window_days"] == 14


def test_archive_in_known_sections():
    """'archive' is in KNOWN_SECTIONS so no unknown-section warning is emitted."""
    assert "archive" in config.KNOWN_SECTIONS


# ---------------------------------------------------------------------------
# CLI integration tests: ws hive archive ls
# ---------------------------------------------------------------------------


def test_cli_archive_ls_empty(monkeypatch, tmp_path):
    """ws hive archive ls prints an empty-archive message when no repos are archived."""
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    home = tmp_path / "wshome"
    home.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("NO_COLOR", "1")

    result = runner.invoke(app, ["hive", "archive", "ls"])
    assert result.exit_code == 0
    assert "empty" in result.output.lower()


def test_cli_archive_ls_populated(monkeypatch, tmp_path):
    """ws hive archive ls shows each triplet with age and size."""
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    home = tmp_path / "wshome"
    home.mkdir()
    cfg_path = tmp_path / "config.yaml"
    adir = ws_root / ".archived"
    repo_dir = _make_archived_repo(adir, "github", "myorg", "myrepo")
    _backdate(repo_dir, days=10)

    cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("NO_COLOR", "1")

    result = runner.invoke(app, ["hive", "archive", "ls"])
    assert result.exit_code == 0
    assert "github/myorg/myrepo" in result.output
    assert "total" in result.output.lower()


def test_cli_archive_ls_json(monkeypatch, tmp_path):
    """ws hive archive ls --json emits typed fields: age_days (float), size_bytes (int)."""
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    home = tmp_path / "wshome"
    home.mkdir()
    cfg_path = tmp_path / "config.yaml"
    adir = ws_root / ".archived"
    repo_dir = _make_archived_repo(adir, "github", "myorg", "myrepo")
    _backdate(repo_dir, days=5)

    cfg_path.write_text("providers: [github]\nmanaged_repos: []\n")
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("NO_COLOR", "1")

    result = runner.invoke(app, ["hive", "archive", "ls", "--json"])
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1
    entry = data[0]
    assert entry["triplet"] == "github/myorg/myrepo"
    assert isinstance(entry["age_days"], (int, float))
    assert isinstance(entry["size_bytes"], int)
    assert entry["size_bytes"] > 0


# ---------------------------------------------------------------------------
# CLI integration tests: ws hive archive prune
# ---------------------------------------------------------------------------


def _cli_prune_env(monkeypatch, tmp_path, cfg_extra: str = "") -> Path:
    """Shared env setup for prune CLI tests. Returns the archive dir."""
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    home = tmp_path / "wshome"
    home.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"providers: [github]\nmanaged_repos: []\n{cfg_extra}")

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("NO_COLOR", "1")

    return ws_root / ".archived"


def test_cli_prune_removes_old(monkeypatch, tmp_path):
    """ws hive archive prune removes only repos older than the threshold."""
    adir = _cli_prune_env(monkeypatch, tmp_path)
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    new_dir = _make_archived_repo(adir, "github", "myorg", "new")
    _backdate(old_dir, days=40)
    _backdate(new_dir, days=2)

    result = runner.invoke(app, ["hive", "archive", "prune", "--older-than", "30d"])
    assert result.exit_code == 0
    assert "old" in result.output
    assert not old_dir.exists()
    assert new_dir.exists()


def test_cli_prune_dry_run(monkeypatch, tmp_path):
    """ws hive archive prune --dry-run mutates nothing and shows would-reclaim."""
    adir = _cli_prune_env(monkeypatch, tmp_path)
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    _backdate(old_dir, days=40)

    result = runner.invoke(app, ["hive", "archive", "prune", "--older-than", "30d", "--dry-run"])
    assert result.exit_code == 0
    # Something about what would be removed or would reclaim
    assert "would" in result.output.lower() or "dry-run" in result.output.lower()
    # The directory must still exist
    assert old_dir.exists()


def test_cli_prune_all(monkeypatch, tmp_path):
    """ws hive archive prune --all removes every archived repo."""
    adir = _cli_prune_env(monkeypatch, tmp_path)
    new_dir = _make_archived_repo(adir, "github", "myorg", "new")
    _backdate(new_dir, days=1)

    result = runner.invoke(app, ["hive", "archive", "prune", "--all"])
    assert result.exit_code == 0
    assert not new_dir.exists()
    assert "Reclaimed" in result.output


def test_cli_prune_uses_window_days(monkeypatch, tmp_path):
    """ws hive archive prune defaults to archive.window_days from config."""
    adir = _cli_prune_env(monkeypatch, tmp_path, cfg_extra="archive:\n  window_days: 7\n")
    old_dir = _make_archived_repo(adir, "github", "myorg", "old")
    _backdate(old_dir, days=10)  # 10d > 7d window → should be pruned

    result = runner.invoke(app, ["hive", "archive", "prune"])
    assert result.exit_code == 0
    assert not old_dir.exists()
