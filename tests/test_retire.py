"""Tests for ws.retire — worktree teardown helper for the retire flow.

Each test provisions a real temporary git repo + managed worktrees (the same pattern used
by test_worktree.py's _ensure_hive helper), monkeypatches config.load so that teardown
helpers resolve the right hive, then exercises teardown_worktrees under three scenarios:

  - clean worktree  → removed, appears in result.removed, parent dirs reclaimed
  - dirty worktree  → skipped, appears in result.dirty, dir still exists
  - dry_run=True    → appears in result.removed but nothing is actually removed
"""

from __future__ import annotations

import os
from pathlib import Path

from beadhive import config, worktree
from beadhive.retire import TeardownResult, teardown_worktrees
from beadhive.run import run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


def _retire_hive(tmp_path, monkeypatch):
    """A real one-commit hive clone with isolated HOME + monkeypatched config.load.

    Returns (cfg, entry, repo_path) — cfg is the same dict that config.load() will return
    so worktree.ensure and teardown_worktrees see a consistent view.
    """
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    wts_root = tmp_path / "wts"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    # Isolate HOME so global ~/.gitconfig doesn't interfere with git ops.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    cfg = {"managed_repos": [entry]}

    # Patch config.load so teardown_worktrees (and worktree.remove inside it) resolves
    # the hive correctly without needing an actual ~/.ws/config.yaml on disk.
    monkeypatch.setattr("beadhive.config.load", lambda: cfg)

    return cfg, entry, repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_teardown_clean_worktree_removes_it(tmp_path, monkeypatch):
    """A clean managed worktree is removed and its path appears in result.removed."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    result = teardown_worktrees("mr")

    assert isinstance(result, TeardownResult)
    assert str(target) in result.removed
    assert not target.exists()
    assert result.dirty == []


def test_teardown_clean_worktree_reclaims_empty_dirs(tmp_path, monkeypatch):
    """After removing the last worktree, empty triplet dirs under the shadow root are
    reclaimed and reported in result.reclaimed_dirs."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    # Confirm the shadow root exists before teardown.
    wts_root = config.worktrees_root().resolve()
    assert (wts_root / "github").exists()

    result = teardown_worktrees("mr")

    # At least one parent dir should have been reclaimed.
    assert result.reclaimed_dirs, "expected at least one empty dir to be reclaimed"
    # All reclaimed dirs must no longer exist.
    for d in result.reclaimed_dirs:
        assert not Path(d).exists(), f"{d} should have been removed"


def test_teardown_dirty_worktree_is_skipped_and_flagged(tmp_path, monkeypatch):
    """A worktree with uncommitted changes is not removed; it appears in result.dirty."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    # Create an untracked file to make the worktree dirty.
    (target / "unsaved.txt").write_text("work in progress")

    result = teardown_worktrees("mr")

    assert str(target) in result.dirty
    assert target.exists()  # not removed
    assert result.removed == []


def test_teardown_dry_run_previews_without_removing(tmp_path, monkeypatch):
    """dry_run=True populates result.removed with what would be removed but leaves
    the worktree dir untouched."""
    cfg, _entry, _repo = _retire_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "retire-test")

    result = teardown_worktrees("mr", dry_run=True)

    assert str(target) in result.removed
    assert target.exists()  # dry_run: nothing actually removed
    assert result.dirty == []
    assert result.reclaimed_dirs == []  # reclaimed_dirs only populated on real removal


# ---------------------------------------------------------------------------
# Generic plugin notify loop (bead .7) — WARN-ONLY, dry-run does not record
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from beadhive import config as _config  # noqa: E402
from beadhive import plugins, registry, retire, safety  # noqa: E402
from beadhive.safety import RetireVerdict  # noqa: E402


def _retire_plugin_setup(tmp_path, monkeypatch):
    """A SAFE, worktree-free hive wired so ``retire_hive`` reaches the plugin notify loop."""
    cfg, entry, repo = _retire_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "resolve_hive", lambda c, hive: entry)
    monkeypatch.setattr(
        safety, "assess_retire",
        lambda p: SimpleNamespace(verdict=RetireVerdict.SAFE, reasons=[]),
    )
    monkeypatch.setattr(registry, "unregister", lambda *a, **k: None)
    return cfg, entry, repo


def test_plugins_notified_includes_orca_when_enabled(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)

    plan = retire.retire_hive("mr")

    assert plan.plugins_notified == ["orca"]


def test_plugins_not_notified_when_orca_disabled(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: False)

    plan = retire.retire_hive("mr")

    assert plan.plugins_notified == []


def test_dry_run_does_not_append_to_plugins_notified(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)

    plan = retire.retire_hive("mr", dry_run=True)

    assert plan.plugins_notified == []


def test_retire_never_writes_orca_data(tmp_path, monkeypatch):
    """orca has no de-registration verb: retire is WARN-ONLY and never touches orca-data.json."""
    _retire_plugin_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_config, "orca_enabled", lambda c, e=None: True)
    data = tmp_path / "orca-data.json"
    data.write_text('{"repos": [{"path": "/x"}]}')
    monkeypatch.setattr(_config, "orca_data_path", lambda c=None: data)
    before = data.read_text()

    retire.retire_hive("mr")

    assert data.read_text() == before  # file untouched under any flag combination


def test_raising_on_retire_hook_is_fenced(tmp_path, monkeypatch):
    _retire_plugin_setup(tmp_path, monkeypatch)
    import typer

    def boom(clone_path, cfg, entry):
        raise RuntimeError("plugin exploded")

    fake = plugins.Plugin(
        name="boom", cli=typer.Typer(), enabled=lambda cfg, entry: True, on_retire=boom,
    )
    monkeypatch.setattr(plugins, "registry", lambda: [fake])

    plan = retire.retire_hive("mr")

    # Fenced: retire completed, the failing plugin is not recorded as notified.
    assert plan.unregistered is True
    assert plan.plugins_notified == []
