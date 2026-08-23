"""Best-effort fences for the optional herdr plugin."""

from __future__ import annotations

import importlib
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from beadhive import herdr_plugin, plugins
from beadhive.cli import app

runner = CliRunner()


def _result(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_import_is_safe_without_herdr_on_path(monkeypatch):
    """Mirror orca's optional-tool fence: import never probes or requires the binary."""
    monkeypatch.setenv("PATH", "")
    importlib.reload(herdr_plugin)


def test_registry_includes_herdr():
    assert "herdr" in [plugin.name for plugin in plugins.registry()]


def test_enabled_requires_binary_and_live_server(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result())
    assert herdr_plugin.PLUGIN.enabled({}, None) is True

    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result(1))
    assert herdr_plugin.PLUGIN.enabled({}, None) is False


def test_enabled_is_false_when_binary_missing_without_subprocess(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*args, **kwargs):
        raise AssertionError("missing herdr must not spawn a subprocess")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    assert herdr_plugin.PLUGIN.enabled({}, None) is False


def test_status_reports_server_and_integrations(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "status":
            return _result(stdout="server ready")
        return _result(stdout="claude: installed\ncodex: absent")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "status"])

    assert result.exit_code == 0, result.output
    assert "herdr: server=up" in result.output
    assert "claude: installed" in result.output
    assert calls == [["herdr", "status"], ["herdr", "integration", "status"]]


def test_status_fences_missing_binary_and_stopped_server(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)
    result = runner.invoke(app, ["plugin", "herdr", "status"])
    assert result.exit_code == 0, result.output
    assert "server=down" in result.output

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result(1, stderr="stopped"))
    result = runner.invoke(app, ["plugin", "herdr", "status"])
    assert result.exit_code == 0, result.output
    assert "server=down" in result.output
    assert "integrations: unavailable" in result.output


def test_ps_lists_tagged_and_unmanaged_agents_from_json(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout='{"agents":[{"name":"bh-bh-123.4","state":"working",'
                '"workspace":{"label":"bh:github/beadhive/beadhive"}},'
                '{"name":"manual","status":"idle"}]}'
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "ps"])

    assert result.exit_code == 0, result.output
    assert "bh-bh-123.4\tgithub/beadhive/beadhive\tbh-123.4\tworking" in result.output
    assert "manual\tunmanaged\tunmanaged\tidle" in result.output


def test_ps_falls_back_to_snapshot_and_fences_server(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(1, stderr="unknown option")
        if argv[-2:] == ["agent", "list"]:
            return _result(1, stderr="unsupported")
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout='{"agents":[{"name":"bh-bh-xyz","state":"done"}]}')
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "ps"])
    assert result.exit_code == 0, result.output
    assert "bh-bh-xyz\tunmanaged\tbh-xyz\tdone" in result.output
    assert calls[:1] == [["herdr", "status"]]

    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _result(1))
    result = runner.invoke(app, ["plugin", "herdr", "ps"])
    assert result.exit_code == 0
    assert "server=down" in result.output


def test_ps_accepts_valid_empty_agent_list_without_snapshot(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(stdout='{"agents": []}')
        raise AssertionError(f"empty list must not fall back: {argv}")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "ps"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["name\thive\tbead\tstate"]
    assert calls == [
        ["herdr", "status"],
        ["herdr", "--session", "bh-supervisor", "agent", "list", "--json"],
    ]


def test_ps_only_claims_exact_reserved_bh_bead_names(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout='{"agents": [{"name": "bh-bh-good.2", "state": "idle"},'
                '{"name": "bh-operator", "state": "idle"},'
                '{"name": "operator-bh-other", "state": "idle"}]}'
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "ps"])
    assert result.exit_code == 0, result.output
    assert "bh-bh-good.2\tunmanaged\tbh-good.2\tidle" in result.output
    assert "bh-operator\tunmanaged\tunmanaged\tidle" in result.output
    assert "operator-bh-other\tunmanaged\tunmanaged\tidle" in result.output


def test_integrate_installs_requested_supported_kind_idempotently(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "agent", "start", "--help"]:
            return _result(stdout="--kind <KIND>  [possible values: claude, codex]")
        return _result(stdout="already installed")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    first = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])
    second = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "integration installed for claude" in second.output
    assert calls == [
        ["herdr", "agent", "start", "--help"],
        ["herdr", "integration", "install", "claude"],
        ["herdr", "agent", "start", "--help"],
        ["herdr", "integration", "install", "claude"],
    ]


def test_integrate_rejects_unsupported_kind_with_discovered_list(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _result(stdout="Supported agent kinds: claude, codex")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "integrate", "gemini"])

    assert result.exit_code == 2
    assert "unsupported agent kind 'gemini'" in result.output
    assert "supported kinds: claude, codex" in result.output
    assert calls == [["herdr", "agent", "start", "--help"]]


def test_integrate_fences_missing_binary_without_subprocess(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*args, **kwargs):
        raise AssertionError("missing herdr must not spawn a subprocess")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    result = runner.invoke(app, ["plugin", "herdr", "integrate", "claude"])

    assert result.exit_code == 1
    assert "herdr CLI not on PATH" in result.output


def _spawn_worktree(tmp_path, monkeypatch):
    target = tmp_path / "bh-1"
    target.mkdir()
    (target / ".git").write_text("gitdir: /tmp/nowhere\n")
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {})
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "locate",
        lambda *_args: ({}, tmp_path, target, "wt/bead/issue/bh-1"),
    )
    return target


def test_spawn_uses_bh_worktree_names_pane_and_verifies_warmup(tmp_path, monkeypatch):
    target = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if argv[-2:] == ["workspace", "create"]:
            return _result(stdout="w1")
        if "workspace" in argv and "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "read" in argv:
            return _result(stdout="assistant: BH_HERDR_WARMUP_OK")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "spawn",
            "--hive",
            "github/beadhive/beadhive",
            "--bead",
            "bh-1",
            "--kind",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(target) in [item for call in calls for item in call]
    assert [
        "herdr",
        "--session",
        "bh-supervisor",
        "agent",
        "start",
        "bh-bh-1",
        "--kind",
        "codex",
        "--pane",
        "w1:p2",
    ] in calls
    assert ["herdr", "--session", "bh-supervisor", "pane", "rename", "w1:p2", "bh-bh-1"] in calls
    assert any(
        "prompt" in call and any("BH_HERDR_WARMUP_OK" in item for item in call) for call in calls
    )
    assert "target=bh-bh-1" in result.output
    assert not any("worktree" in call for call in calls)


def test_spawn_retries_after_first_run_dialog_and_refuses_unreadable_prompt(tmp_path, monkeypatch):
    _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    reads = iter(["onboarding dialog", "still no conversational turn"])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "workspace" in argv and "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "claude"]
    )

    assert result.exit_code == 1
    assert "did not reach an idle agent prompt" in result.output
    assert any("send-keys" in call and "esc" in call for call in calls)
    assert sum("prompt" in call for call in calls) == 2
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls


def test_spawn_closes_new_pane_when_setup_fails(tmp_path, monkeypatch):
    _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "workspace" in argv and "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "rename" in argv:
            return _result(1, stderr="rename failed")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )

    assert result.exit_code == 1
    assert "pane rename failed" in result.output
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls


def test_spawn_closes_pane_when_agent_start_fails(tmp_path, monkeypatch):
    _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "workspace" in argv and "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "start" in argv:
            return _result(1, stderr="agent start failed")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )

    assert result.exit_code == 1
    assert "agent start failed" in result.output
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls


def test_spawn_fences_missing_server_before_worktree_lookup(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*_args):
        raise AssertionError("must not resolve a worktree while herdr is unavailable")

    monkeypatch.setattr(herdr_plugin.worktree, "locate", boom)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )
    assert result.exit_code == 1
    assert "server=down" in result.output


def test_attach_only_prints_a_copy_pasteable_session_scoped_command(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("attach must not inspect or alter herdr state")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    result = runner.invoke(app, ["plugin", "herdr", "attach", "bh-bh-1"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "herdr --session bh-supervisor agent attach bh-bh-1"


def test_reap_closes_only_the_exact_pane_recorded_for_a_spawned_target(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": [{"name": "bh-bh-1", "state": "idle", '
                    '"pane_id": "w1:p2", "pane_name": "bh-bh-1"}]}'
                )
            )
        if "pane" in argv and "close" in argv:
            return _result()
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 0, result.output
    assert "reaped pane=w1:p2" in result.output
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls
    assert not any("workspace" in call or "worktree" in call for call in calls)


def test_reap_refuses_an_unmanaged_target_without_closing_anything(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "operator-agent"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_watch_waits_for_blocked_and_translates_timeout(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result(stdout="agent_status: blocked")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "watch", "bh-agent", "--timeout", "2.5"])

    assert result.exit_code == 0, result.output
    assert "agent_status: blocked" in result.output
    assert calls == [
        ((["herdr", "status"]), {"check": False, "capture": True}),
        (
            [
                "herdr",
                "--session",
                "bh-supervisor",
                "agent",
                "wait",
                "bh-agent",
                "--until",
                "blocked",
                "--timeout",
                "2500",
            ],
            {"check": False, "capture": True, "timeout": 2.5},
        ),
    ]


def test_watch_fences_missing_binary_before_wait(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)

    def boom(*args, **kwargs):
        raise AssertionError("missing herdr must not spawn a subprocess")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    result = runner.invoke(app, ["plugin", "herdr", "watch", "bh-agent"])

    assert result.exit_code == 1
    assert "herdr CLI not on PATH" in result.output


def test_watch_reports_a_bounded_wait_timeout(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "watch", "bh-agent", "--timeout", "1"])

    assert result.exit_code == 1
    assert "timed out after 1s" in result.output
