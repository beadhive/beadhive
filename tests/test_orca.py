"""orca.py — the first bh plugin: repo discovery + orca registration.

Hermetic: no real orca CLI or real $GIT_WORKSPACE. ``shutil.which`` and ``orca.run.out`` are
faked to exercise both the CLI path and the orca-data.json file fallback. Asserts orca-data.json
reads only ever surface the ``repos`` list — never ``projects`` / ``projectHostSetups`` / any
orch db — except the worktree-delegation wiring's deliberate, CLI-only ``orca project
setups``/``setup-update`` exception (see the module docstring).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

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


def test_list_repos_unwraps_cli_envelope(monkeypatch):
    """'orca repo list --json' wraps output in {id, ok, result: {repos: [...]}} — must unwrap."""
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    envelope = {"id": "1", "ok": True, "result": {"repos": [{"path": "/x"}, {"path": "/y"}]}}
    monkeypatch.setattr(orca.run, "out", lambda cmd, **k: json.dumps(envelope))
    assert [r["path"] for r in orca.list_repos()] == ["/x", "/y"]


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


def test_sync_second_run_idempotent_via_cli_envelope(tmp_path, monkeypatch):
    """Regression: the CLI's {result: {repos}} envelope must not make list_repos always return
    [], which would make every sync report 'N registered, 0 already known' forever."""
    root = _fake_workspace(tmp_path, monkeypatch, [
        ("github", "acme", "api"),
        ("github", "acme", "ui"),
    ])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    store: list[str] = []
    monkeypatch.setattr(
        orca.run, "out",
        lambda cmd, **k: json.dumps({"id": "1", "ok": True,
                                      "result": {"repos": [{"path": p} for p in store]}}),
    )
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


def test_readiness_registered_via_cli_envelope(tmp_path, monkeypatch):
    """Regression: a registered clone must read 'registered' from the CLI's envelope shape,
    not fall through to 'missing' because list_repos silently returned []."""
    root = tmp_path / "ws"
    monkeypatch.setattr(orca, "workspace_root", lambda: str(root))
    clone = root / "github" / "acme" / "api"
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    envelope = {"id": "1", "ok": True, "result": {"repos": [{"path": str(clone)}]}}
    monkeypatch.setattr(orca.run, "out", lambda cmd, **k: json.dumps(envelope))
    entry = {"provider": "github", "org": "acme", "repo": "api"}
    assert orca._readiness({}, entry) == ("ok", "registered")


def test_readiness_registered_via_cli_envelope_reaches_worktrees_probe(tmp_path, monkeypatch):
    """With the worktrees flag on, a registered clone (via the CLI envelope) must proceed to
    the runtime probe instead of short-circuiting at 'missing'."""
    root = tmp_path / "ws"
    monkeypatch.setattr(orca, "workspace_root", lambda: str(root))
    clone = root / "github" / "acme" / "api"
    monkeypatch.setattr(orca.shutil, "which", lambda _name: "/usr/bin/orca")
    envelope = {"id": "1", "ok": True, "result": {"repos": [{"path": str(clone)}]}}
    monkeypatch.setattr(orca.run, "out", lambda cmd, **k: json.dumps(envelope))
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=True, state="ready"))
    entry = {"provider": "github", "org": "acme", "repo": "api"}
    state, detail = orca._readiness(_wt_cfg(), entry)
    assert state == "ok"
    assert "worktree delegation ready" in detail


def test_readiness_missing_when_not_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "workspace_root", lambda: str(tmp_path / "ws"))
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])
    entry = {"provider": "github", "org": "acme", "repo": "api"}
    state, detail = orca._readiness({}, entry)
    assert state == "missing"


# ---- readiness: worktree delegation probe -----------------------------------

_WT_ENTRY = {"provider": "github", "org": "acme", "repo": "api"}


def _wt_cfg(*, fallback=False) -> dict:
    return {
        "git_workspace": {"enabled": True},
        "orca": {"enabled": True, "worktrees": {"enabled": True, "fallback": fallback}},
    }


def _register_clone(tmp_path, monkeypatch):
    """Fake workspace_root + list_repos so the rig's clone is already registered."""
    root = tmp_path / "ws"
    monkeypatch.setattr(orca, "workspace_root", lambda: str(root))
    clone = root / "github" / "acme" / "api"
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": str(clone)}])


def _fake_status(*, reachable, state):
    payload = json.dumps({"result": {"runtime": {"reachable": reachable, "state": state}}})
    return lambda cmd, **k: SimpleNamespace(returncode=0, stdout=payload)


def test_readiness_flag_off_skips_runtime_probe(tmp_path, monkeypatch):
    """worktrees flag off → no probe at all, plain registered readiness."""
    _register_clone(tmp_path, monkeypatch)

    def boom(cmd, **k):
        raise AssertionError("must not probe the orca runtime when worktrees flag is off")

    monkeypatch.setattr(orca.run, "run", boom)
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}  # no worktrees flag
    assert orca._readiness(cfg, _WT_ENTRY) == ("ok", "registered")


def test_readiness_ok_when_runtime_ready(tmp_path, monkeypatch):
    _register_clone(tmp_path, monkeypatch)
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=True, state="ready"))
    state, detail = orca._readiness(_wt_cfg(), _WT_ENTRY)
    assert state == "ok"
    assert "worktree delegation ready" in detail


def test_readiness_warn_when_runtime_down_hard_fail_by_default(tmp_path, monkeypatch):
    _register_clone(tmp_path, monkeypatch)
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=False, state="down"))
    state, detail = orca._readiness(_wt_cfg(fallback=False), _WT_ENTRY)
    assert state == "warn"
    assert "delegated worktree ops will fail" in detail


def test_readiness_warn_when_runtime_down_with_fallback(tmp_path, monkeypatch):
    _register_clone(tmp_path, monkeypatch)
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=False, state="down"))
    state, detail = orca._readiness(_wt_cfg(fallback=True), _WT_ENTRY)
    assert state == "warn"
    assert "fall back to native git" in detail


def test_readiness_warn_when_runtime_probe_raises(tmp_path, monkeypatch):
    """Subprocess failure (missing CLI, timeout, ...) degrades to warn — never raises."""
    _register_clone(tmp_path, monkeypatch)
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})

    def boom(cmd, **k):
        raise FileNotFoundError("orca not on PATH")

    monkeypatch.setattr(orca.run, "run", boom)
    state, _detail = orca._readiness(_wt_cfg(), _WT_ENTRY)
    assert state == "warn"


def test_readiness_warn_when_auto_rename_enabled(tmp_path, monkeypatch):
    _register_clone(tmp_path, monkeypatch)
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": True}})
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=True, state="ready"))
    state, detail = orca._readiness(_wt_cfg(), _WT_ENTRY)
    assert state == "warn"
    assert "autoRenameBranchFromWork" in detail


def test_readiness_never_writes_data_file_on_auto_rename_warn(tmp_path, monkeypatch):
    """PARSE-AND-WARN ONLY — the data file must be untouched even when it triggers a warning."""
    _register_clone(tmp_path, monkeypatch)
    p = _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": True}})
    before = p.read_text()
    monkeypatch.setattr(orca.run, "run", _fake_status(reachable=True, state="ready"))
    orca._readiness(_wt_cfg(), _WT_ENTRY)
    assert p.read_text() == before


# ---- remove_worktree (wt_remove hook) ---------------------------------------


def _rm_cfg(*, fallback=False) -> dict:
    return {
        "git_workspace": {"enabled": True},
        "orca": {"enabled": True, "worktrees": {"enabled": True, "fallback": fallback}},
    }


def _fake_rm_subprocess(calls):
    """Fakes orca.run.run: records every call, always answers 'removed: true' for the rm
    itself (git checkout --detach calls return an unrelated ok/empty-stdout result)."""

    def fake(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["orca", "worktree", "rm"]:
            ok_payload = json.dumps({"ok": True, "result": {"removed": True}})
            return SimpleNamespace(returncode=0, stdout=ok_payload)
        return SimpleNamespace(returncode=0, stdout="")

    return fake


def test_remove_worktree_flag_off_returns_false_without_invoking_orca(monkeypatch):
    def boom(cmd, **k):
        raise AssertionError("must not invoke orca when the worktrees flag is off")

    monkeypatch.setattr(orca.run, "run", boom)
    cfg = {"git_workspace": {"enabled": True}, "orca": {"enabled": True}}  # no worktrees flag
    result = orca.remove_worktree(
        cfg, _WT_ENTRY, main=Path("/m"), target=Path("/t"), force=True, keep_branch=True
    )
    assert result is False


def test_remove_worktree_success_returns_true_without_native_removal(monkeypatch):
    """Confirmed success (exit 0 + ok:true/removed:true) -> True; the hook itself never issues a
    native 'git worktree remove' (that's what lets the seam skip native removal entirely)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(orca.run, "run", _fake_rm_subprocess(calls))
    result = orca.remove_worktree(
        _rm_cfg(), _WT_ENTRY, main=Path("/m"), target=Path("/t"), force=True, keep_branch=False
    )
    assert result is True
    assert not any("remove" in c for c in calls)  # no native 'worktree remove' subprocess


def test_remove_worktree_keep_branch_true_detaches_before_rm(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(orca.run, "run", _fake_rm_subprocess(calls))
    result = orca.remove_worktree(
        _rm_cfg(), _WT_ENTRY, main=Path("/m"), target=Path("/t"), force=True, keep_branch=True
    )
    assert result is True
    assert calls[0] == ["git", "-C", "/t", "checkout", "--detach"]
    assert calls[1][:3] == ["orca", "worktree", "rm"]


def test_remove_worktree_keep_branch_false_skips_detach(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(orca.run, "run", _fake_rm_subprocess(calls))
    orca.remove_worktree(
        _rm_cfg(), _WT_ENTRY, main=Path("/m"), target=Path("/t"), force=True, keep_branch=False
    )
    assert len(calls) == 1  # only the rm call — no detach
    assert calls[0][:3] == ["orca", "worktree", "rm"]


def test_remove_worktree_omits_force_flag_when_force_false(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(orca.run, "run", _fake_rm_subprocess(calls))
    orca.remove_worktree(
        _rm_cfg(), _WT_ENTRY, main=Path("/m"), target=Path("/t"), force=False, keep_branch=False
    )
    assert "--force" not in calls[0]


def test_remove_worktree_detach_failure_is_hard_fail_by_default(monkeypatch):
    monkeypatch.setattr(orca.run, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(typer.Exit):
        orca.remove_worktree(
            _rm_cfg(fallback=False),
            _WT_ENTRY,
            main=Path("/m"),
            target=Path("/t"),
            force=True,
            keep_branch=True,
        )


def test_remove_worktree_detach_failure_with_fallback_warns_and_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(orca.run, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stdout=""))
    result = orca.remove_worktree(
        _rm_cfg(fallback=True),
        _WT_ENTRY,
        main=Path("/m"),
        target=Path("/t"),
        force=True,
        keep_branch=True,
    )
    assert result is False
    assert "stale" in capsys.readouterr().err.lower()


def test_remove_worktree_orca_rm_failure_is_hard_fail_by_default(monkeypatch):
    err_payload = json.dumps({"ok": False, "error": {"code": "x"}})

    def fake(cmd, **k):
        return SimpleNamespace(returncode=1, stdout=err_payload)

    monkeypatch.setattr(orca.run, "run", fake)
    with pytest.raises(typer.Exit):
        orca.remove_worktree(
            _rm_cfg(fallback=False),
            _WT_ENTRY,
            main=Path("/m"),
            target=Path("/t"),
            force=True,
            keep_branch=False,
        )


def test_remove_worktree_orca_rm_failure_with_fallback_warns_and_returns_false(monkeypatch, capsys):
    def fake(cmd, **k):
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(orca.run, "run", fake)
    result = orca.remove_worktree(
        _rm_cfg(fallback=True),
        _WT_ENTRY,
        main=Path("/m"),
        target=Path("/t"),
        force=True,
        keep_branch=False,
    )
    assert result is False
    assert "falling back to native removal" in capsys.readouterr().err.lower()


def test_remove_worktree_runtime_down_hard_fails_by_default(monkeypatch):
    def boom(cmd, **k):
        raise FileNotFoundError("orca not on PATH")

    monkeypatch.setattr(orca.run, "run", boom)
    with pytest.raises(typer.Exit):
        orca.remove_worktree(
            _rm_cfg(fallback=False),
            _WT_ENTRY,
            main=Path("/m"),
            target=Path("/t"),
            force=True,
            keep_branch=False,
        )


def test_remove_worktree_runtime_down_with_fallback_lets_native_removal_proceed(monkeypatch):
    def boom(cmd, **k):
        raise FileNotFoundError("orca not on PATH")

    monkeypatch.setattr(orca.run, "run", boom)
    result = orca.remove_worktree(
        _rm_cfg(fallback=True),
        _WT_ENTRY,
        main=Path("/m"),
        target=Path("/t"),
        force=True,
        keep_branch=False,
    )
    assert result is False  # caller (the seam) falls through to native removal


# ---- PLUGIN wiring ----------------------------------------------------------


def test_plugin_registered_in_registry():
    from beadhive import plugins

    names = {p.name for p in plugins.registry()}
    assert "orca" in names


def test_plugin_wt_remove_wired_to_remove_worktree():
    assert orca.PLUGIN.wt_remove is orca.remove_worktree


def test_plugin_registers_create_worktree_as_wt_create():
    from beadhive import plugins

    (mine,) = [p for p in plugins.registry() if p.name == "orca"]
    assert mine.wt_create is orca.create_worktree


# ---- create_worktree (wt_create hook) ---------------------------------------


class _FakeGit:
    """Fake subprocess dispatcher for `create_worktree`'s git + orca CLI calls, keyed off the
    argv shape rather than a fixed call sequence (matches the module's own dispatch order:
    git for-each-ref (pre-create snapshot) -> orca create -> git rev-parse (fixup's
    already-exists check) -> git branch/checkout (fixup) -> git branch --show-current
    (verify) -> orca rm (cleanup, only on a failure path)."""

    def __init__(self, *, existing_branches=(), create_result=None):
        self.existing_branches = set(existing_branches)  # pre-create snapshot, in `main`
        self.current_branch = None  # set once orca "creates" the tree
        self.create_result = create_result  # (returncode, stdout) for the orca create call
        self.calls: list[list[str]] = []
        self.cleanup_calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:3] == ["orca", "worktree", "create"]:
            leaf = cmd[cmd.index("--name") + 1]
            rc, stdout = self.create_result
            branch = leaf
            if rc == 0:
                try:
                    worktree = (json.loads(stdout).get("result") or {}).get("worktree") or {}
                    branch = str(worktree.get("branch") or leaf).removeprefix("refs/heads/")
                except Exception:  # noqa: BLE001 - bad JSON just falls back to the leaf
                    branch = leaf
            self.current_branch = branch  # orca checks out the (possibly prefixed) branch
            return SimpleNamespace(returncode=rc, stdout=stdout)
        if cmd[:3] == ["orca", "worktree", "rm"]:
            self.cleanup_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout='{"ok":true}')
        if cmd[0] == "git" and "for-each-ref" in cmd:
            stdout = "".join(f"{name}\n" for name in sorted(self.existing_branches))
            return SimpleNamespace(returncode=0, stdout=stdout)
        if cmd[0] == "git" and "rev-parse" in cmd:
            ref = cmd[-1].removeprefix("refs/heads/")
            found = ref in self.existing_branches or ref == self.current_branch
            return SimpleNamespace(returncode=0 if found else 1, stdout="")
        if cmd[0] == "git" and "branch" in cmd and "-m" in cmd:
            _, new = cmd[-2], cmd[-1]
            self.current_branch = new
            return SimpleNamespace(returncode=0, stdout="")
        if cmd[0] == "git" and "checkout" in cmd:
            target_branch = cmd[-1] if "-b" not in cmd else cmd[cmd.index("-b") + 1]
            self.current_branch = target_branch
            return SimpleNamespace(returncode=0, stdout="")
        if cmd[0] == "git" and "--show-current" in cmd:
            return SimpleNamespace(returncode=0, stdout=(self.current_branch or "") + "\n")
        raise AssertionError(f"unexpected subprocess call: {cmd}")


def _wt_create_args(tmp_path, *, branch="wt/bead/issue/x-1", leaf="x-1", start_point=""):
    main = tmp_path / "main"
    target = tmp_path / "wts" / leaf
    return {"main": main, "branch": branch, "target": target, "start_point": start_point}


def _ok_envelope(path, branch="x-1") -> str:
    return json.dumps(
        {"ok": True, "result": {"worktree": {"path": str(path), "branch": f"refs/heads/{branch}"}}}
    )


def test_create_worktree_flag_off_returns_none_without_any_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: False)

    def boom(cmd, **kw):
        raise AssertionError("must not shell out when worktrees flag is off")

    monkeypatch.setattr(orca.run, "run", boom)
    args = _wt_create_args(tmp_path)
    assert orca.create_worktree({}, {}, **args) is None


def test_create_worktree_happy_path_renames_new_leaf_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    args = _wt_create_args(tmp_path)
    fake = _FakeGit(create_result=(0, _ok_envelope(args["target"])))
    monkeypatch.setattr(orca.run, "run", fake)

    result = orca.create_worktree({}, {}, **args)

    assert result == args["target"]
    assert any(c[:3] == ["orca", "worktree", "create"] for c in fake.calls)
    rename_calls = [c for c in fake.calls if c[0] == "git" and "-m" in c]
    expected = ["git", "-C", str(args["target"]), "branch", "-m", "x-1", args["branch"]]
    assert rename_calls == [expected]
    assert fake.cleanup_calls == []  # happy path never cleans up


def test_create_worktree_prefixed_created_branch_is_renamed_to_bh_branch(tmp_path, monkeypatch):
    """orca's global branchPrefix setting can hand back `<username>/<leaf>` instead of `<leaf>`
    (the live-e2e finding) — the fixup must key off the ACTUAL branch the
    create response reports, not assume it equals the requested leaf."""
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    args = _wt_create_args(tmp_path)
    fake = _FakeGit(create_result=(0, _ok_envelope(args["target"], branch="briancripe/x-1")))
    monkeypatch.setattr(orca.run, "run", fake)

    result = orca.create_worktree({}, {}, **args)

    assert result == args["target"]
    rename_calls = [c for c in fake.calls if c[0] == "git" and "-m" in c]
    expected = ["git", "-C", str(args["target"]), "branch", "-m", "briancripe/x-1", args["branch"]]
    assert rename_calls == [expected]
    assert fake.cleanup_calls == []


def test_create_worktree_path_mismatch_cleans_up_and_hard_fails_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    monkeypatch.setattr(orca.config, "orca_worktrees_fallback", lambda cfg=None: False)
    args = _wt_create_args(tmp_path)
    wrong_path = tmp_path / "elsewhere" / "x-1"
    fake = _FakeGit(create_result=(0, _ok_envelope(wrong_path)))
    monkeypatch.setattr(orca.run, "run", fake)

    with pytest.raises(typer.Exit) as exc:
        orca.create_worktree({}, {}, **args)
    assert exc.value.exit_code == 1
    assert fake.cleanup_calls and f"path:{wrong_path}" in fake.cleanup_calls[0]


def test_create_worktree_path_mismatch_falls_back_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    monkeypatch.setattr(orca.config, "orca_worktrees_fallback", lambda cfg=None: True)
    args = _wt_create_args(tmp_path)
    wrong_path = tmp_path / "elsewhere" / "x-1"
    fake = _FakeGit(create_result=(0, _ok_envelope(wrong_path)))
    monkeypatch.setattr(orca.run, "run", fake)

    result = orca.create_worktree({}, {}, **args)

    assert result is None
    assert fake.cleanup_calls  # cleanup still fired
    assert "falling back to native" in capsys.readouterr().err


def test_create_worktree_runtime_down_hard_fails_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    monkeypatch.setattr(orca.config, "orca_worktrees_fallback", lambda cfg=None: False)
    args = _wt_create_args(tmp_path)

    def down(cmd, **kw):
        raise FileNotFoundError("orca not on PATH")

    monkeypatch.setattr(orca.run, "run", down)
    with pytest.raises(typer.Exit) as exc:
        orca.create_worktree({}, {}, **args)
    assert exc.value.exit_code == 1


def test_create_worktree_nonok_result_falls_back_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    monkeypatch.setattr(orca.config, "orca_worktrees_fallback", lambda cfg=None: True)
    args = _wt_create_args(tmp_path)

    def bad_json(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout="not json")

    monkeypatch.setattr(orca.run, "run", bad_json)
    result = orca.create_worktree({}, {}, **args)
    assert result is None
    assert "falling back to native" in capsys.readouterr().err


def test_create_worktree_preexisting_leaf_branch_is_never_renamed(tmp_path, monkeypatch):
    """The spike's 'existing branch name' finding: a leaf branch that pre-exists gets attached
    by orca, not renamed — the fixup must checkout/create `branch` instead, leaving `<leaf>`
    untouched."""
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    args = _wt_create_args(tmp_path)
    fake = _FakeGit(existing_branches={"x-1"}, create_result=(0, _ok_envelope(args["target"])))
    monkeypatch.setattr(orca.run, "run", fake)

    result = orca.create_worktree({}, {}, **args)

    assert result == args["target"]
    assert not any(c[0] == "git" and "-m" in c for c in fake.calls)  # leaf never renamed
    checkout_b = [c for c in fake.calls if c[0] == "git" and "-b" in c]
    assert checkout_b == [["git", "-C", str(args["target"]), "checkout", "-b", args["branch"]]]


def test_create_worktree_fixup_failure_cleans_up_and_hard_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.config, "orca_worktrees_enabled", lambda cfg, entry=None: True)
    monkeypatch.setattr(orca.config, "orca_worktrees_fallback", lambda cfg=None: False)
    args = _wt_create_args(tmp_path)
    fake = _FakeGit(create_result=(0, _ok_envelope(args["target"])))

    def flaky(cmd, **kw):
        if cmd[0] == "git" and "-m" in cmd:
            return SimpleNamespace(returncode=1, stdout="")  # rename silently fails to stick
        return fake(cmd, **kw)

    monkeypatch.setattr(orca.run, "run", flaky)
    with pytest.raises(typer.Exit):
        orca.create_worktree({}, {}, **args)
    assert fake.cleanup_calls  # best-effort cleanup still fired


# ---- _ensure_worktree_base_path (onboard/sync worktree-delegation wiring) ---

_WIRE_ENTRY = {"provider": "github", "org": "acme", "repo": "api"}


def _wire_cfg(*, worktrees=True) -> dict:
    return {"git_workspace": {"enabled": True}, "orca": {"enabled": True, "worktrees": worktrees}}


def test_ensure_worktree_base_path_noop_when_flag_off(monkeypatch):
    def boom(cmd, **k):
        raise AssertionError("must not touch orca when the worktrees flag is off")

    monkeypatch.setattr(orca.run, "run", boom)
    orca._ensure_worktree_base_path(_wire_cfg(worktrees=False), _WIRE_ENTRY, Path("/clone"))


def test_ensure_worktree_base_path_warns_on_auto_rename(no_cli, tmp_path, monkeypatch, capsys):
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": True}})
    orca._ensure_worktree_base_path(_wire_cfg(), _WIRE_ENTRY, Path("/clone"))
    err = capsys.readouterr().err
    assert "autoRenameBranchFromWork" in err
    assert "Settings UI" in err


def test_ensure_worktree_base_path_skips_cli_calls_without_cli(no_cli, tmp_path, monkeypatch):
    _write_data(tmp_path, monkeypatch, {"settings": {}})

    def boom(cmd, **k):
        raise AssertionError("no CLI on PATH -> must not shell out")

    monkeypatch.setattr(orca.run, "run", boom)
    orca._ensure_worktree_base_path(_wire_cfg(), _WIRE_ENTRY, Path("/clone"))


def _fake_setups_and_update(calls, *, setups, update_ok=True):
    def fake(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["orca", "project", "setups"]:
            payload = json.dumps({"ok": True, "result": {"setups": setups}})
            return SimpleNamespace(returncode=0, stdout=payload)
        if cmd[:3] == ["orca", "project", "setup-update"]:
            payload = json.dumps({"ok": update_ok, "result": {}})
            return SimpleNamespace(returncode=0, stdout=payload)
        raise AssertionError(f"unexpected call: {cmd}")

    return fake


def test_ensure_worktree_base_path_updates_setup_when_found(tmp_path, monkeypatch):
    monkeypatch.setattr(orca.shutil, "which", lambda _n: "/usr/bin/orca")
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    clone = Path("/ws/github/acme/api")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        orca.run, "run", _fake_setups_and_update(calls, setups=[{"id": "s1", "path": str(clone)}])
    )
    monkeypatch.setattr(orca.config, "worktrees_root", lambda cfg=None: Path("/wts"))

    orca._ensure_worktree_base_path(_wire_cfg(), _WIRE_ENTRY, clone)

    update_calls = [c for c in calls if c[:3] == ["orca", "project", "setup-update"]]
    assert update_calls == [
        [
            "orca", "project", "setup-update", "--setup", "s1",
            "--worktree-base-path", str(Path("/wts") / "github" / "acme"), "--json",
        ]
    ]


def test_ensure_worktree_base_path_warns_when_setup_not_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orca.shutil, "which", lambda _n: "/usr/bin/orca")
    _write_data(tmp_path, monkeypatch, {"settings": {"autoRenameBranchFromWork": False}})
    clone = Path("/ws/github/acme/api")
    calls: list[list[str]] = []
    monkeypatch.setattr(orca.run, "run", _fake_setups_and_update(calls, setups=[]))

    orca._ensure_worktree_base_path(_wire_cfg(), _WIRE_ENTRY, clone)

    assert not any(c[:3] == ["orca", "project", "setup-update"] for c in calls)
    assert "no project-setup found" in capsys.readouterr().err


# ---- _on_onboard (register + worktree-delegation wiring) --------------------


def test_on_onboard_registers_and_wires_worktree_base_path(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(orca, "add_repo", lambda path, cfg=None: calls.append((path, cfg)))
    wired: list[tuple] = []
    monkeypatch.setattr(
        orca, "_ensure_worktree_base_path",
        lambda cfg, entry, clone: wired.append((cfg, entry, clone)),
    )
    ctx = SimpleNamespace(
        base=Path("/ws/github/acme/api"), cfg={"x": 1}, provider="github", org="acme", repo="api"
    )

    orca._on_onboard(ctx)

    assert calls == [(str(ctx.base), {"x": 1})]
    assert wired == [({"x": 1}, {"provider": "github", "org": "acme", "repo": "api"}, ctx.base)]


# ---- sync_repos worktree-delegation wiring -----------------------------------


def test_sync_wires_worktree_base_path_for_managed_rigs(tmp_path, monkeypatch):
    root = _fake_workspace(tmp_path, monkeypatch, [("github", "acme", "api")])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    clone = root / "github" / "acme" / "api"
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": str(clone)}])
    from beadhive import registry

    entry = {"provider": "github", "org": "acme", "repo": "api"}
    monkeypatch.setattr(registry, "find_entry", lambda cfg, p, o, r: entry)
    wired: list[tuple] = []
    monkeypatch.setattr(
        orca, "_ensure_worktree_base_path", lambda cfg, e, c: wired.append((cfg, e, c))
    )

    orca.sync_repos({"managed_repos": []})

    assert wired == [({"managed_repos": []}, entry, clone)]


def test_sync_skips_wiring_for_unmanaged_clones(tmp_path, monkeypatch):
    _fake_workspace(tmp_path, monkeypatch, [("github", "acme", "api")])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])
    from beadhive import registry

    monkeypatch.setattr(registry, "find_entry", lambda cfg, p, o, r: None)

    def boom(*a, **k):
        raise AssertionError("must not wire an unmanaged clone")

    monkeypatch.setattr(orca, "_ensure_worktree_base_path", boom)
    orca.sync_repos({"managed_repos": []})


def test_sync_worktree_wiring_safely_skips_a_deeper_than_3_level_clone(tmp_path, monkeypatch):
    """bh-4y0r.2 bug fix: a clone path nested deeper than <group>/<org>/<repo> must be safely
    skipped (no wiring call), never silently mis-mapped by truncating to its first 3 segments."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    from beadhive import registry

    deep = tmp_path / "gitlab" / "group" / "subgroup" / "repo"
    deep.mkdir(parents=True)

    def boom(*a, **k):
        raise AssertionError("must not wire a mis-mapped deep-nested clone")

    monkeypatch.setattr(registry, "find_entry", boom)
    orca._sync_worktree_wiring({"managed_repos": []}, deep)  # no raise, no wiring call


def test_sync_dry_run_never_wires(tmp_path, monkeypatch):
    _fake_workspace(tmp_path, monkeypatch, [("github", "acme", "api")])
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])

    def boom(*a, **k):
        raise AssertionError("dry_run must never wire")

    monkeypatch.setattr(orca, "_ensure_worktree_base_path", boom)
    orca.sync_repos({"managed_repos": []}, dry_run=True)


# ---- fix_settings (bh plugin orca fix-settings) ------------------------------


def test_fix_settings_refuses_when_runtime_up(monkeypatch, capsys):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: True)
    with pytest.raises(typer.Exit) as exc:
        orca.fix_settings()
    assert exc.value.exit_code == 1
    assert "Settings UI" in capsys.readouterr().err


def test_fix_settings_flips_value_when_runtime_down(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: False)
    p = _write_data(tmp_path, monkeypatch, {
        "settings": {"autoRenameBranchFromWork": True, "other": "keep-me"},
        "repos": [{"path": "/a"}],
    })

    result = orca.fix_settings()

    assert result is True
    data = json.loads(p.read_text())
    assert data["settings"]["autoRenameBranchFromWork"] is False
    assert data["settings"]["other"] == "keep-me"
    assert data["repos"] == [{"path": "/a"}]


def test_fix_settings_creates_settings_key_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: False)
    p = _write_data(tmp_path, monkeypatch, {"repos": []})

    orca.fix_settings()

    data = json.loads(p.read_text())
    assert data["settings"]["autoRenameBranchFromWork"] is False


def test_fix_settings_refuses_on_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: False)
    monkeypatch.setattr(orca.config, "orca_data_path", lambda cfg=None: tmp_path / "absent.json")
    with pytest.raises(typer.Exit):
        orca.fix_settings()


def test_fix_settings_writes_atomically_no_tmp_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: False)
    _write_data(tmp_path, monkeypatch, {"settings": {}})

    orca.fix_settings()

    assert list(tmp_path.glob("*.tmp")) == []


# ---- warn_retire (modernized: names the real setup-delete verb) -------------


def test_warn_retire_names_setup_delete_command(no_cli, tmp_path, monkeypatch, capsys):
    _write_data(tmp_path, monkeypatch, {"repos": [{"path": "/a"}]})
    orca.warn_retire("/a")
    err = capsys.readouterr().err
    assert "orca project setup-delete" in err
    assert "no de-registration verb" not in err
