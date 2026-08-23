# ruff: noqa: E501, E701
"""Optional herdr integration fences and command behavior."""

from types import SimpleNamespace

from typer.testing import CliRunner

from beadhive import herdr_plugin
from beadhive.cli import app

runner = CliRunner()


def _result(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _spawn_worktree(tmp_path, monkeypatch):
    target = tmp_path / "bh-1"
    target.mkdir()
    (target / ".git").write_text("gitdir: /tmp/nowhere\n")
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {})
    monkeypatch.setattr(
        herdr_plugin.worktree, "locate", lambda *_: ({}, tmp_path, target, "wt/bead/issue/bh-1")
    )
    return target


def test_integrate_discovers_and_installs_kind(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _: "/bin/herdr")
    calls = []

    def fake(argv, **_):
        calls.append(argv)
        return _result(
            stdout="[possible values: claude, codex]" if "--help" in argv else "installed"
        )

    monkeypatch.setattr(herdr_plugin.run, "run", fake)
    result = runner.invoke(app, ["plugin", "herdr", "integrate", "codex"])
    assert result.exit_code == 0, result.output
    assert ["herdr", "integration", "install", "codex"] in calls


def test_integrate_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _: "/bin/herdr")
    monkeypatch.setattr(
        herdr_plugin.run, "run", lambda *_a, **_k: _result(stdout="Supported agent kinds: claude")
    )
    assert runner.invoke(app, ["plugin", "herdr", "integrate", "bad"]).exit_code == 2


def test_spawn_warms_agent_and_names_pane(tmp_path, monkeypatch):
    target = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _: "/bin/herdr")
    calls = []

    def fake(argv, **_):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "read" in argv:
            return _result(stdout="BH_HERDR_WARMUP_OK")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )
    assert result.exit_code == 0, result.output
    assert str(target) in [x for call in calls for x in call]
    assert any("rename" in call and "bh-bh-1" in call for call in calls)
    assert any("BH_HERDR_WARMUP_OK" in " ".join(call) for call in calls)


def test_spawn_closes_pane_on_warmup_or_rename_failure(tmp_path, monkeypatch):
    _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _: "/bin/herdr")
    calls = []

    def fake(argv, **_):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "rename" in argv:
            return _result(1, stderr="no")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake)
    assert (
        runner.invoke(
            app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
        ).exit_code
        == 1
    )
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls
