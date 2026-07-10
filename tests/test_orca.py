"""orca.py — the first bh plugin: repo discovery + orca registration.

Hermetic: no real orca CLI or real $GIT_WORKSPACE. ``shutil.which`` and ``orca.run.out`` are
faked to exercise both the CLI path and the orca-data.json file fallback. Asserts orca only
ever surfaces the ``repos`` list — never ``projects`` / ``projectHostSetups`` / any orch db.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beadhive import orca


@pytest.fixture
def no_cli(monkeypatch):
    """Force the file-fallback path: orca CLI not on PATH."""
    monkeypatch.setattr(orca.shutil, "which", lambda _name: None)


def _write_data(tmp_path, monkeypatch, payload) -> Path:
    """Write an orca-data.json and point config.orca_data_path at it."""
    p = tmp_path / "orca-data.json"
    p.write_text(json.dumps(payload))
    monkeypatch.setattr(orca.config, "orca_data_path", lambda cfg=None: p)
    return p


def _fake_workspace(tmp_path, monkeypatch, triplets, *, with_git=True):
    """Build a fake $GIT_WORKSPACE tree of provider/org/repo dirs and point workspace_root at it."""
    root = tmp_path / "ws"
    for provider, org, repo in triplets:
        d = root / provider / org / repo
        d.mkdir(parents=True)
        if with_git:
            (d / ".git").mkdir()
    monkeypatch.setattr(orca, "workspace_root", lambda: str(root))
    return root


# ---- is_available -----------------------------------------------------------


def test_is_available_false_when_no_cli_and_no_file(no_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_data_path", lambda cfg=None: tmp_path / "absent.json")
    assert orca.is_available() is False


def test_is_available_true_when_file_exists(no_cli, tmp_path, monkeypatch):
    _write_data(tmp_path, monkeypatch, {"repos": []})
    assert orca.is_available() is True


def test_is_available_true_when_cli_present(monkeypatch, tmp_path):
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    monkeypatch.setattr(orca.config, "orca_data_path", lambda cfg=None: tmp_path / "absent.json")
    assert orca.is_available() is True


# ---- list_repos (file fallback + scope guard) -------------------------------


def test_list_repos_file_fallback(no_cli, tmp_path, monkeypatch):
    _write_data(tmp_path, monkeypatch, {"repos": [{"path": "/a"}, {"path": "/b"}]})
    assert [r["path"] for r in orca.list_repos()] == ["/a", "/b"]


def test_list_repos_never_reads_projects(no_cli, tmp_path, monkeypatch):
    """Only repos[] is surfaced — projects[] / projectHostSetups[] are ignored entirely."""
    _write_data(tmp_path, monkeypatch, {
        "repos": [{"path": "/a"}],
        "projects": [{"path": "/should-not-appear"}],
        "projectHostSetups": [{"path": "/nope"}],
    })
    paths = {r.get("path") for r in orca.list_repos()}
    assert paths == {"/a"}


def test_list_repos_empty_on_unreadable_file(no_cli, tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_data_path", lambda cfg=None: tmp_path / "absent.json")
    assert orca.list_repos() == []


def test_list_repos_uses_cli_when_present(monkeypatch):
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    monkeypatch.setattr(orca.run, "out", lambda cmd, **k: json.dumps([{"path": "/x"}]))
    assert [r["path"] for r in orca.list_repos()] == ["/x"]


# ---- add_repo ---------------------------------------------------------------


def test_add_repo_idempotent_when_already_known(no_cli, tmp_path, monkeypatch):
    _write_data(tmp_path, monkeypatch, {"repos": [{"path": "/known"}]})
    assert orca.add_repo("/known") is False


def test_add_repo_false_when_no_cli_and_new(no_cli, tmp_path, monkeypatch):
    _write_data(tmp_path, monkeypatch, {"repos": []})
    assert orca.add_repo("/new") is False


def test_add_repo_new_with_faked_subprocess(monkeypatch):
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    calls: list[list[str]] = []

    def fake_out(cmd, **k):
        calls.append(cmd)
        if "list" in cmd:
            return "[]"
        return "{}"

    monkeypatch.setattr(orca.run, "out", fake_out)
    assert orca.add_repo("/new") is True
    assert ["orca", "repo", "add", "--path", "/new", "--json"] in calls


def test_add_repo_false_on_subprocess_failure(monkeypatch):
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")

    def fake_out(cmd, **k):
        if "list" in cmd:
            return "[]"
        raise RuntimeError("orca add exploded")

    monkeypatch.setattr(orca.run, "out", fake_out)
    assert orca.add_repo("/new") is False


# ---- discover_repos ---------------------------------------------------------


def test_discover_repos_walks_three_levels(tmp_path, monkeypatch):
    root = _fake_workspace(tmp_path, monkeypatch, [
        ("github", "acme", "api"),
        ("github", "acme", "ui"),
        ("gitlab", "org", "lib"),
    ])
    found = {str(p) for p in orca.discover_repos()}
    assert found == {
        str(root / "github" / "acme" / "api"),
        str(root / "github" / "acme" / "ui"),
        str(root / "gitlab" / "org" / "lib"),
    }


def test_discover_repos_skips_dirs_without_git(tmp_path, monkeypatch):
    _fake_workspace(tmp_path, monkeypatch, [("github", "acme", "api")], with_git=False)
    assert orca.discover_repos() == []


def test_discover_repos_empty_when_root_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "workspace_root", lambda: str(tmp_path / "nope"))
    assert orca.discover_repos() == []


# ---- sync_repos -------------------------------------------------------------


def test_sync_unavailable(monkeypatch):
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: False)
    result = orca.sync_repos()
    assert result.unavailable is True
    assert result.added == [] and result.checked == []


def test_sync_adds_then_idempotent(tmp_path, monkeypatch):
    root = _fake_workspace(tmp_path, monkeypatch, [
        ("github", "acme", "api"),
        ("github", "acme", "ui"),
    ])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    store: list[str] = []
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": p} for p in store])
    monkeypatch.setattr(orca, "add_repo", lambda p, cfg=None: (store.append(str(p)) or True))

    first = orca.sync_repos()
    assert set(first.added) == {
        str(root / "github" / "acme" / "api"),
        str(root / "github" / "acme" / "ui"),
    }
    assert first.skipped == []

    second = orca.sync_repos()
    assert second.added == []
    assert set(second.skipped) == set(first.added)


def test_sync_dry_run_would_add_without_calling_add(tmp_path, monkeypatch):
    _fake_workspace(tmp_path, monkeypatch, [("github", "acme", "api")])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])

    def boom(*a, **k):
        raise AssertionError("add_repo must not run under dry_run")

    monkeypatch.setattr(orca, "add_repo", boom)
    result = orca.sync_repos(dry_run=True)
    assert len(result.added) == 1


# ---- warn_retire (no mutation) ----------------------------------------------


def test_warn_retire_does_not_mutate_data(no_cli, tmp_path, monkeypatch, capsys):
    p = _write_data(tmp_path, monkeypatch, {"repos": [{"path": "/a"}]})
    before = p.read_text()
    orca.warn_retire("/a")
    assert p.read_text() == before  # file untouched
    assert "manual" in capsys.readouterr().err.lower() or "remove" in capsys.readouterr().err


# ---- readiness --------------------------------------------------------------


def test_readiness_none_without_triplet():
    assert orca._readiness({}, {}) is None


def test_readiness_ok_when_registered(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    monkeypatch.setattr(orca, "workspace_root", lambda: str(root))
    clone = root / "github" / "acme" / "api"
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": str(clone)}])
    entry = {"provider": "github", "org": "acme", "repo": "api"}
    assert orca._readiness({}, entry) == ("ok", "registered")


def test_readiness_missing_when_not_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "workspace_root", lambda: str(tmp_path / "ws"))
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])
    entry = {"provider": "github", "org": "acme", "repo": "api"}
    state, detail = orca._readiness({}, entry)
    assert state == "missing"


# ---- PLUGIN wiring ----------------------------------------------------------


def test_plugin_registered_in_registry():
    from beadhive import plugins

    names = {p.name for p in plugins.registry()}
    assert "orca" in names
