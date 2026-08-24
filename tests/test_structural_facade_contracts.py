"""Compatibility contracts for the large modules targeted by the structural program.

These are deliberately facade tests.  They prove that callers can keep importing from the
original modules and that the collaborator names tests patch there are looked up at runtime.
An extraction may move implementations, but it must preserve these seams (or migrate them in an
equally explicit compatibility change).
"""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from beadhive import config, work, worktree


def test_work_issue_facade_executes_the_module_local_bd_patch_point(monkeypatch):
    calls = []

    def fake_bd(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='[{"id": "bh-contract", "status": "open"}]\n',
            stderr="",
        )

    monkeypatch.setattr(work.bd, "_run", fake_bd)
    monkeypatch.setattr(work.config, "load", lambda: {"sentinel": "effective-config"})
    monkeypatch.setattr(
        work.registry,
        "hive_dir_for",
        lambda cfg, hive: "/facade/hive" if cfg["sentinel"] and hive == "" else None,
    )

    result = CliRunner().invoke(work.app, ["issue", "bh-contract", "--json"])

    assert result.exit_code == 0
    assert result.stdout == '[{"id": "bh-contract", "status": "open"}]\n'
    assert calls[0][0] == ["bd", "-C", "/facade/hive", "show", "bh-contract", "--json"]
    assert calls[0][1]["capture"] is True


def test_config_load_facade_executes_layer_patch_points_and_preserves_precedence(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "load_fleet", lambda: calls.append("fleet") or {"shared": 1})
    monkeypatch.setattr(
        config,
        "load_host",
        lambda: calls.append("host") or {"local": 2},
    )
    monkeypatch.setattr(
        config,
        "_reject_fleet_overrides",
        lambda host: calls.append(("guard", dict(host))),
    )

    effective = config.load()

    assert effective == {"shared": 1, "local": 2}
    assert calls == ["fleet", "host", ("guard", {"local": 2})]


def test_worktree_run_init_facade_executes_the_module_local_runner(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worktree, "run", fake_run)

    worktree.run_init(
        {"worktrees": {"init": [{"run": "tool --flag value", "verify": True}]}},
        {},
        tmp_path,
        verify_only=True,
    )

    assert calls == [(["tool", "--flag", "value"], {"cwd": str(tmp_path), "check": False})]


def test_worktree_classifier_facade_forwards_payload_and_callback_patch_points(monkeypatch):
    rows = [("mr", "/worktrees/bh-contract", "wt/bead/issue/bh-contract")]
    captured = {}
    meta = SimpleNamespace(branches=["main", "wt/bead/issue/bh-contract"])

    monkeypatch.setattr(worktree.registry, "hive_key", lambda entry: "github/acme/repo")
    monkeypatch.setattr(
        "beadhive.metadata.read_fleet",
        lambda cfg, keys, ttl: {keys[0]: meta} if cfg == {"cfg": True} and ttl == 0 else {},
    )
    monkeypatch.setattr(worktree.config, "integration_branch", lambda cfg, entry: "main")
    monkeypatch.setattr(
        worktree,
        "_bead_statuses_for_entry",
        lambda entry, actual_rows: (
            {"bh-contract": "closed"},
            {"bh-contract": "merged"},
            {},
            "",
        ),
    )
    monkeypatch.setattr(worktree, "_wt_dirty", lambda path: path.endswith("dirty"))
    monkeypatch.setattr(worktree, "is_merged", lambda entry, branch, base: (branch, base))
    monkeypatch.setattr(
        worktree,
        "bead_and_parent",
        lambda entry, path, integration, branch="": ("bh-contract", integration),
    )
    monkeypatch.setattr(
        worktree,
        "is_landed",
        lambda entry, branch, base, close_reason="": close_reason == "merged",
    )
    monkeypatch.setattr(
        worktree.wt_status,
        "classify",
        lambda **kwargs: captured.update(kwargs) or ["classified"],
    )

    result = worktree._classify_entry({"prefix": "mr"}, rows, {"cfg": True})

    assert result == ["classified"]
    assert captured["hive_prefix"] == "mr"
    assert captured["managed_rows"] == rows
    assert captured["meta_branches"] == meta.branches
    assert captured["bead_statuses"] == {"bh-contract": "closed"}
    assert captured["bead_close_reasons"] == {"bh-contract": "merged"}
    assert captured["integration"] == "main"
    assert captured["is_merged_fn"](None, "topic", "main") == ("topic", "main")
    assert captured["parent_fn"](None, "/somewhere", "main") == ("bh-contract", "main")
    assert captured["is_landed_fn"](None, "topic", "main", "merged") is True


def test_worktree_pid_start_uses_its_documented_subprocess_patch_point(monkeypatch):
    monkeypatch.setattr(
        worktree,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("wrong patch point")),
    )
    monkeypatch.setattr(
        worktree.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout=" start-token \n"),
    )

    assert worktree._pid_start(4321) == "start-token"
