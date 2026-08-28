"""Best-effort fences for the optional herdr plugin."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import typer
from typer.testing import CliRunner

from beadhive import guard, herdr_plugin, plugins, registry, work
from beadhive.cli import app

runner = CliRunner()
_LIFECYCLE_SCHEMA_PATH = (
    Path(__file__).parents[1] / "docs" / "schemas" / "herdr-lifecycle-receipt-v1.schema.json"
)


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
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "codex"])
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "locate",
        lambda *_args: ({}, tmp_path, target, "wt/bead/issue/bh-1"),
    )
    return target


def test_resolve_kind_precedence_is_explicit_then_hive_then_global(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "codex"])
    entry = {"herdr": {"kind": "codex"}}

    assert herdr_plugin._resolve_kind(None, {"herdr": {"kind": "claude"}}, entry) == "codex"
    assert herdr_plugin._resolve_kind("claude", {"herdr": {"kind": "codex"}}, entry) == "claude"


def test_resolve_kind_uses_harness_then_claude_without_host_order(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["codex", "claude", "future"])

    assert herdr_plugin._resolve_kind(None, {"harness": "codex"}, {}) == "codex"
    assert herdr_plugin._resolve_kind(None, {"harness": "opencode"}, {}) == "claude"


def test_resolve_kind_rejects_unsupported_config_with_remedy(monkeypatch, capsys):
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "future"])

    with pytest.raises(typer.Exit) as exc_info:
        herdr_plugin._resolve_kind(None, {"herdr": {"kind": "codex"}}, {})

    assert exc_info.value.exit_code == 2
    error = capsys.readouterr().err
    assert "supported kinds: claude, future" in error
    assert "change herdr.kind or pass --kind" in error


def test_resolve_kind_accepts_externally_supported_future_value(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "future-agent"])

    assert (
        herdr_plugin._resolve_kind(None, {"herdr": {"kind": "future-agent"}}, {}) == "future-agent"
    )


def test_spawn_uses_bh_worktree_names_pane_and_verifies_warmup(tmp_path, monkeypatch):
    target = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(
                stdout='{"id":"cli:api:snapshot","result":{"snapshot":'
                '{"workspaces":[],"layouts":[],"panes":[]},"type":"session_snapshot"}}'
            )
        if "workspace" in argv and "create" in argv:
            return _result(
                stdout='{"id":"cli:workspace:create","result":'
                '{"workspace":{"workspace_id":"w1"},'
                '"tab":{"tab_id":"w1:t1"},"root_pane":{"pane_id":"w1:p1"},'
                '"type":"workspace_created"}}'
            )
        if "split" in argv:
            return _result(
                stdout='{"id":"cli:pane:split","result":'
                '{"pane":{"pane_id":"w1:p2"},"type":"pane_info"}}'
            )
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


def test_spawn_reuses_snapshot_workspace_and_its_actual_pane(tmp_path, monkeypatch):
    target = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(
                stdout='{"id":"cli:api:snapshot","result":{"snapshot":'
                '{"workspaces":[{"label":"bh:h","workspace_id":"w9"}],'
                '"layouts":[{"workspace_id":"w9","focused_pane_id":"w9:p7"}],'
                '"panes":[{"workspace_id":"w9","pane_id":"w9:p7"}]},'
                '"type":"session_snapshot"}}'
            )
        if "split" in argv:
            return _result(
                stdout='{"id":"cli:pane:split","result":'
                '{"pane":{"pane_id":"w9:p8"},"type":"pane_info"}}'
            )
        if "read" in argv:
            return _result(stdout="assistant: BH_HERDR_WARMUP_OK")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )

    assert result.exit_code == 0, result.output
    assert not any("workspace" in call and "create" in call for call in calls)
    assert [
        "herdr",
        "--session",
        "bh-supervisor",
        "pane",
        "split",
        "--pane",
        "w9:p7",
        "--direction",
        "right",
        "--cwd",
        str(target),
        "--no-focus",
    ] in calls
    assert "pane=w9:p8 workspace=w9" in result.output


def test_spawn_rejects_structured_create_without_root_pane_id(tmp_path, monkeypatch):
    _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout='{"id":"cli:api:snapshot","result":{"snapshot":{}}}')
        if "workspace" in argv and "create" in argv:
            return _result(
                stdout='{"id":"cli:workspace:create","result":'
                '{"workspace":{"workspace_id":"w1"},"root_pane":{}}}'
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )

    assert result.exit_code == 1
    assert "workspace create failed: response missing pane_id" in result.output
    assert not any("split" in call for call in calls)


def test_spawn_rejects_structured_split_without_pane_id(tmp_path, monkeypatch):
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
            return _result(stdout='{"id":"cli:pane:split","result":{"pane":{}}}')
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app, ["plugin", "herdr", "spawn", "--hive", "h", "--bead", "bh-1", "--kind", "codex"]
    )

    assert result.exit_code == 1
    assert "pane split failed: response missing pane_id" in result.output
    assert not any("agent" in call and "start" in call for call in calls)


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
            return _result(
                stdout='{"id":"cli:workspace:create","result":'
                '{"workspace":{"workspace_id":"w1"},'
                '"root_pane":{"pane_id":"w1:p1"}}}'
            )
        if "split" in argv:
            return _result(stdout='{"id":"cli:pane:split","result":{"pane":{"pane_id":"w1:p2"}}}')
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


def test_dispatch_verifies_a_new_prompt_landed_for_claude_and_codex(monkeypatch):
    """Both supported harnesses pass only when the post-read adds the real turn."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    for kind in ("claude", "codex"):
        prompt = f"{kind} please implement the requested change"
        calls = []
        reads = iter(["agent: idle", f"user: {prompt}\\nassistant: working"])

        def fake_run(argv, _calls=calls, _reads=reads, **kwargs):
            _calls.append(argv)
            if argv == ["herdr", "status"]:
                return _result()
            if "read" in argv:
                return _result(stdout=next(_reads))
            return _result(stdout="agent_status: done")

        monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
        result = runner.invoke(app, ["plugin", "herdr", "dispatch", f"{kind}-1", prompt])

        assert result.exit_code == 0, result.output
        assert [
            "herdr",
            "--session",
            "bh-supervisor",
            "agent",
            "prompt",
            f"{kind}-1",
            prompt,
            "--wait",
            "--timeout",
            "60000",
        ] in calls
        assert sum("read" in call and "visible" in call for call in calls) == 2


def test_dispatch_rejects_stale_prompt_from_a_prior_turn(monkeypatch):
    """An unchanged pane containing an older identical prompt is not a delivery proof."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    prompt = "implement the requested change"
    reads = iter([f"user: {prompt}\\nassistant: done", f"user: {prompt}\\nassistant: done"])

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: done")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", "codex-1", prompt])

    assert result.exit_code == 1
    assert "did not reach a new real agent turn" in result.output


def test_dispatch_accepts_a_repeated_prompt_only_when_a_new_copy_appears(monkeypatch):
    """Repeating an intentional prompt remains valid, but needs a new pane occurrence."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    prompt = "repeat this prompt"
    reads = iter([f"user: {prompt}", f"user: {prompt}\\nassistant: done\\nuser: {prompt}"])

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: done")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", "claude-1", prompt])

    assert result.exit_code == 0, result.output


def test_dispatch_fails_when_codex_first_run_screen_drops_prompt(monkeypatch):
    """Codex can say done after its hook-review screen consumed the first prompt."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    prompt = "implement the requested change"
    calls = []
    reads = iter(["user: old unrelated work", "Codex hook-review screen: trust hooks? [y/N]"])

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: done")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", "codex-1", prompt])

    assert result.exit_code == 1
    assert "did not reach a new real agent turn" in result.output
    assert any("prompt" in call and "--wait" in call for call in calls)
    assert sum("read" in call and "visible" in call for call in calls) == 2


def _assert_lifecycle_receipt(payload, operation, disposition):
    jsonschema.Draft202012Validator(json.loads(_LIFECYCLE_SCHEMA_PATH.read_text())).validate(
        payload
    )
    assert payload["schema_version"] == 1
    assert payload["command"] == f"plugin herdr {operation}"
    assert payload["operation"] == operation
    assert payload["disposition"] == disposition
    assert payload["operation_id"]
    assert payload["observed_at"].endswith("Z")
    assert payload["session"] == "bh-supervisor"
    for key in (
        "hive",
        "bead",
        "target",
        "workspace",
        "pane",
        "worktree",
        "capabilities",
        "warnings",
        "retained_resources",
    ):
        assert key in payload


def test_lifecycle_status_ps_and_attach_emit_shared_json_receipts(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result(stdout="server ready")
        if argv == ["herdr", "integration", "status"]:
            return _result(stdout="claude: installed\ncodex: not installed")
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout='{"agents":[{"name":"bh-bh-7","state":"working",'
                '"workspace":{"label":"bh:github/acme/widgets"}}]}'
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    status = runner.invoke(
        app, ["plugin", "herdr", "status", "--json", "--operation-id", "status:7"]
    )
    ps = runner.invoke(app, ["plugin", "herdr", "ps", "--json"])
    attach = runner.invoke(
        app,
        ["plugin", "herdr", "attach", "bh-bh-7", "--json", "--operation-id", "attach:7"],
    )

    assert status.exit_code == ps.exit_code == attach.exit_code == 0
    status_payload = json.loads(status.stdout)
    _assert_lifecycle_receipt(status_payload, "status", "available")
    assert status_payload["operation_id"] == "status:7"
    assert status_payload["integrations"] == [
        {"kind": "claude", "state": "installed"},
        {"kind": "codex", "state": "not installed"},
    ]
    ps_payload = json.loads(ps.stdout)
    _assert_lifecycle_receipt(ps_payload, "ps", "listed")
    assert ps_payload["agents"] == [
        {
            "target": "bh-bh-7",
            "hive": "github/acme/widgets",
            "bead": "bh-7",
            "state": "working",
        }
    ]
    attach_payload = json.loads(attach.stdout)
    _assert_lifecycle_receipt(attach_payload, "attach", "instructions")
    assert attach_payload["attach_argv"] == [
        "herdr",
        "--session",
        "bh-supervisor",
        "agent",
        "attach",
        "bh-bh-7",
    ]


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_dispatch_safe_input_preserves_adversarial_multiline_without_leakage(
    source, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    secret = "first line\n$(touch /tmp/never) ' \" ; $TOKEN\nlast\\line"
    reads = iter(["agent: idle", f"agent: idle\nuser: {secret}\nassistant: working"])
    process_argv = []
    socket_prompts = []

    def fake_run(argv, **kwargs):
        process_argv.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        raise AssertionError(argv)

    def socket_prompt(target, prompt, **kwargs):
        socket_prompts.append((target, prompt, kwargs))
        return _result(stdout='{"result":{"type":"agent_prompt","agent_status":"done"}}')

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    monkeypatch.setattr(herdr_plugin, "_prompt_over_socket", socket_prompt)
    argv = ["plugin", "herdr", "dispatch", "bh-bh-7", "--json"]
    input_text = None
    if source == "stdin":
        argv.append("--stdin")
        input_text = secret
    else:
        prompt_path = tmp_path / "prompt.txt"
        prompt_path.write_text(secret)
        argv.extend(["--prompt-file", str(prompt_path)])

    result = runner.invoke(app, argv, input=input_text)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_lifecycle_receipt(payload, "dispatch", "dispatched")
    assert payload["input_source"] == source
    assert payload["delivery_verified"] is True
    assert socket_prompts == [("bh-bh-7", secret, {})]
    assert all(secret not in "\0".join(call) for call in process_argv)
    assert secret not in result.output
    assert secret not in caplog.text


def test_dispatch_json_refusal_is_structured_and_not_retryable(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    prompt = "same prompt"
    reads = iter([f"user: {prompt}", f"user: {prompt}"])

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app,
        ["plugin", "herdr", "dispatch", "bh-bh-7", prompt, "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    _assert_lifecycle_receipt(payload, "dispatch", "refused")
    assert payload["outcome"] == "refused"
    assert payload["error"]["code"] == "dispatch_unverified"
    assert payload["error"]["retryable"] is False
    assert prompt not in result.stdout


def test_watch_timeout_and_reap_refusal_emit_stable_json_errors(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "wait" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(stdout='{"agents": []}')
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    watched = runner.invoke(
        app, ["plugin", "herdr", "watch", "bh-bh-7", "--timeout", "1", "--json"]
    )
    reaped = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-7", "--json"])

    assert watched.exit_code == reaped.exit_code == 1
    watch_payload = json.loads(watched.stdout)
    _assert_lifecycle_receipt(watch_payload, "watch", "timed_out")
    assert watch_payload["error"]["code"] == "watch_timeout"
    assert watch_payload["error"]["retryable"] is True
    reap_payload = json.loads(reaped.stdout)
    _assert_lifecycle_receipt(reap_payload, "reap", "refused")
    assert reap_payload["error"]["code"] == "ownership_not_proven"
    assert reap_payload["retained_resources"] == [{"kind": "target", "id": "bh-bh-7"}]


def test_spawn_watch_and_reap_success_emit_json_dispositions(tmp_path, monkeypatch):
    worktree_path = tmp_path / "bh-7"
    worktree_path.mkdir()
    (worktree_path / ".git").write_text("gitdir: /tmp/example\n")
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {})
    monkeypatch.setattr(herdr_plugin, "_managed_worktree", lambda *_args: ({}, worktree_path))
    monkeypatch.setattr(herdr_plugin, "_resolve_kind", lambda kind, *_args: kind)
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin, "_workspace", lambda *_args: ("w1", "w1:p1"))
    monkeypatch.setattr(herdr_plugin, "_owned_live_pane", lambda _target: "w1:p2")

    def command(*args, **kwargs):
        if args[:2] == ("pane", "split"):
            return _result(stdout='{"pane":{"pane_id":"w1:p2"}}')
        if args[:2] == ("agent", "read"):
            return _result(stdout=f"assistant: {herdr_plugin._WARMUP_TOKEN}")
        if args[:2] == ("agent", "wait"):
            return _result(stdout='{"result":{"agent_status":"blocked"}}')
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)
    spawned = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "spawn",
            "--hive",
            "github/acme/widgets",
            "--bead",
            "bh-7",
            "--kind",
            "codex",
            "--json",
        ],
    )
    watched = runner.invoke(app, ["plugin", "herdr", "watch", "bh-bh-7", "--json"])
    reaped = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-7", "--json"])

    assert spawned.exit_code == watched.exit_code == reaped.exit_code == 0
    spawn_payload = json.loads(spawned.stdout)
    _assert_lifecycle_receipt(spawn_payload, "spawn", "created")
    assert spawn_payload["hive"] == "github/acme/widgets"
    assert spawn_payload["bead"] == "bh-7"
    assert spawn_payload["worktree"] == str(worktree_path)
    watch_payload = json.loads(watched.stdout)
    _assert_lifecycle_receipt(watch_payload, "watch", "settled")
    assert watch_payload["resulting_state"] == "blocked"
    reap_payload = json.loads(reaped.stdout)
    _assert_lifecycle_receipt(reap_payload, "reap", "reaped")
    assert reap_payload["pane"] == "w1:p2"


def test_safe_prompt_socket_request_keeps_body_out_of_process_metadata(monkeypatch):
    secret = "multiline\nsecret $(not-a-shell)\nend"
    sent = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return b'{"id":"x","result":{"type":"agent_prompt","agent_status":"done"}}\n'

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, value):
            sent.append(value)

        def makefile(self, _mode):
            return FakeStream()

    monkeypatch.setattr(herdr_plugin, "_session_socket_path", lambda: (Path("/tmp/herdr.sock"), ""))
    monkeypatch.setattr(herdr_plugin.socket, "socket", lambda *_args: FakeSocket())

    result = herdr_plugin._prompt_over_socket("bh-bh-7", secret)

    assert result.returncode == 0
    request = json.loads(sent[0])
    assert request["method"] == "agent.prompt"
    assert request["params"]["text"] == secret
    assert secret not in "\0".join(result.args)
    assert secret not in result.stdout


def test_lifecycle_receipt_schema_covers_every_json_command():
    schema = json.loads(_LIFECYCLE_SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert set(schema["properties"]["operation"]["enum"]) == {
        "status",
        "ps",
        "spawn",
        "dispatch",
        "watch",
        "attach",
        "reap",
    }


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
            return _result(2, stderr="unexpected argument '--json'")
        if argv[-2:] == ["agent", "list"]:
            return _result(
                stdout=(
                    '{"id":"cli:agent:list","result":{"agents": ['
                    '{"name": "bh-bh-1", "agent_status": "idle", '
                    '"pane_id": "w1:p2", "label": "bh-bh-1"}],'
                    '"type":"agent_list"}}'
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


def test_reap_refuses_a_terminal_or_stale_agent_record(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": [{"name": "bh-bh-1", "state": "done", '
                    '"pane_id": "w1:p2", "pane_name": "bh-bh-1"}]}'
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_reap_refuses_a_pane_whose_visible_name_does_not_match_target(monkeypatch):
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
                    '"pane_id": "w1:p2", "pane_name": "manual-pane"}]}'
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_reap_refuses_duplicate_live_agent_records_for_one_pane(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": ['
                    '{"name": "bh-bh-1", "state": "idle", "pane_id": "w1:p2", '
                    '"pane_name": "bh-bh-1"}, '
                    '{"name": "bh-bh-other", "state": "working", "pane_id": "w1:p2", '
                    '"pane_name": "bh-bh-other"}]}'
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_reap_refuses_an_unnamed_partial_record_claiming_the_same_pane(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": ['
                    '{"name": "bh-bh-1", "state": "idle", "pane_id": "w1:p2", '
                    '"pane_name": "bh-bh-1"}, '
                    '{"pane": {"pane_id": "w1:p2"}}]}'
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_reap_refuses_a_wrapper_agent_with_a_sibling_pane_claim(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": ['
                    '{"name": "bh-bh-1", "state": "idle", "pane_id": "w1:p2", '
                    '"pane_name": "bh-bh-1"}, '
                    '{"agent": {"name": "different", "state": "working"}, '
                    '"pane": {"id": "w1:p2", "name": "different"}}]}'
                )
            )
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any("close" in call for call in calls)


def test_reap_accepts_a_target_whose_pane_is_on_its_agent_wrapper(monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-3:] == ["agent", "list", "--json"]:
            return _result(
                stdout=(
                    '{"agents": [{"agent": {"name": "bh-bh-1", "state": "idle"}, '
                    '"pane": {"id": "w1:p2", "name": "bh-bh-1"}}]}'
                )
            )
        if "pane" in argv and "close" in argv:
            return _result()
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 0, result.output
    assert ["herdr", "--session", "bh-supervisor", "pane", "close", "w1:p2", "--no-focus"] in calls


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


def _launch_fixture(monkeypatch, tmp_path, *, disposition="claimed"):
    """Fence real stores and Herdr while retaining the launch command's orchestration."""
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widgets",
        "prefix": "widget",
    }
    target = tmp_path / "widget-1"
    target.mkdir()
    claim = SimpleNamespace(
        bead={"id": "widget-1", "assignee": "dev/launch"},
        actor="dev/launch",
        disposition=disposition,
        worktree=target,
    )
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"harness": "codex"})
    monkeypatch.setattr(
        registry,
        "resolve_bead_hive",
        lambda *_args, **_kwargs: registry.BeadHiveResolution("widget-1", entry, (entry,)),
    )
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "codex"])
    monkeypatch.setattr(herdr_plugin.config, "herdr_kind", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin.config, "harness_name", lambda *_args: "codex")
    monkeypatch.setattr(herdr_plugin, "_integration_ready", lambda _kind: (True, "current"))
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: {})
    monkeypatch.setattr(herdr_plugin, "_launch_lease", lambda *_args: None)
    monkeypatch.setattr(work, "_claim_single_bead", lambda *_args: claim)
    return entry, claim


def test_launch_fresh_bead_emits_exact_json_and_forwards_layout(tmp_path, monkeypatch):
    _entry, claim = _launch_fixture(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin, "_workspace", lambda *_args: ("w1", "w1:p1"))
    monkeypatch.setattr(herdr_plugin, "_launch_warm", lambda _target: (True, ""))

    def command(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("pane", "split"):
            return _result(stdout='{"pane":{"pane_id":"w1:p2"}}')
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)
    result = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "launch",
            "widget-1",
            "--direction",
            "down",
            "--focus",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "command": "plugin herdr launch",
        "status": "ready",
        "disposition": "created",
        "hive": "github/acme/widgets",
        "bead": "widget-1",
        "kind": "codex",
        "worktree": str(claim.worktree),
        "workspace": "w1",
        "pane": "w1:p2",
        "target": "bh-widget-1",
    }
    split = next(call for call in calls if call[:2] == ("pane", "split"))
    assert split[-2:] == (str(claim.worktree), "--focus")
    assert "down" in split
    assert not any("worktree" in call for call in calls)


def test_launch_same_actor_returns_proven_live_agent_without_new_pane(tmp_path, monkeypatch):
    _entry, claim = _launch_fixture(monkeypatch, tmp_path, disposition="reattached")
    monkeypatch.setattr(
        herdr_plugin,
        "_strict_live_target",
        lambda target, hive, cwd: ("w9", "w9:p7"),
    )

    def boom(*_args):
        raise AssertionError("proven reuse must not create a workspace or pane")

    monkeypatch.setattr(herdr_plugin, "_workspace", boom)
    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 0, result.output
    assert "disposition=reused" in result.output
    assert "workspace=w9" in result.output
    assert "pane=w9:p7" in result.output
    assert str(claim.worktree) in result.output


def test_launch_preflight_finishes_before_native_claim(tmp_path, monkeypatch):
    _entry, _claim = _launch_fixture(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(
        herdr_plugin,
        "_integration_ready",
        lambda kind: events.append("integration") or (True, "current"),
    )
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: events.append("session") or {})
    monkeypatch.setattr(
        herdr_plugin,
        "_launch_lease",
        lambda *_args: events.append("lease"),
    )

    def claim(*_args):
        events.append("claim")
        raise typer.Exit(1)

    monkeypatch.setattr(work, "_claim_single_bead", claim)
    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 1
    assert events == ["integration", "session", "lease", "claim"]
    assert "stage=claim" in result.output


@pytest.mark.parametrize(
    ("stage", "patch"),
    [
        ("cli", "cli"),
        ("integration", "integration"),
        ("session", "session"),
    ],
)
def test_launch_preflight_failure_is_staged_and_never_claims(stage, patch, tmp_path, monkeypatch):
    _launch_fixture(monkeypatch, tmp_path)
    if patch == "cli":
        monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: False)
    elif patch == "integration":
        monkeypatch.setattr(
            herdr_plugin, "_integration_ready", lambda _kind: (False, "not installed")
        )
    else:
        monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: None)
    monkeypatch.setattr(
        work,
        "_claim_single_bead",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 1
    assert f"stage={stage}" in result.output


def test_launch_concurrent_loser_closes_only_its_pane_and_returns_winner(tmp_path, monkeypatch):
    _launch_fixture(monkeypatch, tmp_path)
    lookups = iter([None, ("w1", "w1:p9")])
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: next(lookups))
    monkeypatch.setattr(herdr_plugin, "_workspace", lambda *_args: ("w1", "w1:p1"))
    closed = []
    monkeypatch.setattr(herdr_plugin, "_close_pane", closed.append)

    def command(*args, **_kwargs):
        if args[:2] == ("pane", "split"):
            return _result(stdout="w1:p2")
        if args[:2] == ("agent", "start"):
            return _result(1, stderr="name must be unique; already live")
        raise AssertionError(args)

    monkeypatch.setattr(herdr_plugin, "_command", command)
    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["disposition"] == "reused"
    assert json.loads(result.stdout)["pane"] == "w1:p9"
    assert closed == ["w1:p2"]


def test_launch_warmup_failure_retains_claim_and_only_closes_created_pane(tmp_path, monkeypatch):
    _entry, claim = _launch_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin, "_workspace", lambda *_args: ("w1", "w1:p1"))
    monkeypatch.setattr(herdr_plugin, "_launch_warm", lambda _target: (False, "prompt not visible"))
    closed = []
    monkeypatch.setattr(herdr_plugin, "_close_pane", closed.append)

    def command(*args, **_kwargs):
        if args[:2] == ("pane", "split"):
            return _result(stdout="w1:p2")
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)
    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 1
    assert "stage=warmup" in result.output
    assert f"retained: bead=widget-1 claim=claimed worktree={claim.worktree}" in result.output
    assert "status: bh work issue widget-1 --json" in result.output
    assert "attach: bh plugin herdr attach bh-widget-1" in result.output
    assert "retry: bh plugin herdr launch widget-1" in result.output
    assert closed == ["w1:p2"]


def test_strict_reuse_requires_target_pane_workspace_and_exact_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "wt"
    cwd.mkdir()
    target = "bh-widget-1"
    good = {
        "agents": [{"name": target, "state": "idle", "pane_id": "w1:p2"}],
        "panes": [
            {
                "pane_id": "w1:p2",
                "workspace_id": "w1",
                "label": target,
                "cwd": str(cwd),
            }
        ],
        "workspaces": [{"workspace_id": "w1", "label": "bh:github/acme/widgets"}],
    }
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: good)
    assert herdr_plugin._strict_live_target(target, "github/acme/widgets", cwd) == (
        "w1",
        "w1:p2",
    )

    good["workspaces"][0]["label"] = "bh:github/acme/other"
    with pytest.raises(RuntimeError, match="does not prove"):
        herdr_plugin._strict_live_target(target, "github/acme/widgets", cwd)


def test_launch_target_is_legacy_when_valid_and_hashed_when_encoding_is_needed():
    assert herdr_plugin._launch_target("widget-1") == "bh-widget-1"
    dotted = herdr_plugin._launch_target("widget-1.2")
    long = herdr_plugin._launch_target("widget-" + "x" * 80)
    collision = herdr_plugin._launch_target("widget-1-2")

    assert dotted != collision
    assert dotted == herdr_plugin._launch_target("widget-1.2")
    assert len(long) == 32
    assert herdr_plugin._HERDR_NAME_RE.fullmatch(dotted)
    assert herdr_plugin._HERDR_NAME_RE.fullmatch(long)


def test_launch_lease_adopts_only_expired_state_when_explicit(monkeypatch):
    expired = SimpleNamespace(
        held_by=lambda _host: False,
        is_expired=lambda: True,
        describe=lambda: "expired lease",
    )
    monkeypatch.setattr(guard, "primary_state", lambda *_args, **_kwargs: ("widget", "h1", expired))
    adopted = []
    monkeypatch.setattr(
        herdr_plugin,
        "_adopt_expired_lease",
        lambda cfg, entry: adopted.append((cfg, entry)),
    )

    with pytest.raises(typer.Exit):
        herdr_plugin._launch_lease({}, {"prefix": "widget"}, "github/acme/widgets", False)
    assert adopted == []

    herdr_plugin._launch_lease({}, {"prefix": "widget"}, "github/acme/widgets", True)
    assert adopted == [({}, {"prefix": "widget"})]


def test_launch_lease_never_adopts_a_live_foreign_holder(monkeypatch):
    live = SimpleNamespace(
        held_by=lambda _host: False,
        is_expired=lambda: False,
        describe=lambda: "other-host, epoch 8",
    )
    monkeypatch.setattr(guard, "primary_state", lambda *_args, **_kwargs: ("widget", "h1", live))
    monkeypatch.setattr(
        herdr_plugin,
        "_adopt_expired_lease",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not adopt")),
    )

    with pytest.raises(typer.Exit):
        herdr_plugin._launch_lease({}, {"prefix": "widget"}, "github/acme/widgets", True)


def test_launch_help_leads_with_one_argument_path_and_documents_boundaries():
    result = runner.invoke(app, ["plugin", "herdr", "launch", "--help"])

    assert result.exit_code == 0, result.output
    compact = " ".join(result.output.split())
    assert "launch nvhack-lvxi --json" in compact
    for option in (
        "--hive",
        "--kind",
        "--as",
        "--adopt-expired",
        "--direction",
        "--focus",
        "--no-focus",
        "--json",
    ):
        assert option in result.output
    assert "active foreign host lease" in compact
    assert "never creates or removes a worktree" in compact


def test_spawn_keeps_its_three_required_low_level_options():
    result = runner.invoke(app, ["plugin", "herdr", "spawn", "--help"])

    assert result.exit_code == 0, result.output
    compact = " ".join(result.output.split())
    assert "--hive" in compact
    assert "--bead" in compact
    assert "--kind" in compact
    assert compact.count("[required]") >= 3


@pytest.mark.parametrize("ambiguous", [False, True])
def test_launch_reports_missing_or_ambiguous_bead_before_claim(ambiguous, tmp_path, monkeypatch):
    first = {"provider": "github", "org": "acme", "repo": "one", "prefix": "one"}
    second = {"provider": "github", "org": "acme", "repo": "two", "prefix": "two"}
    _launch_fixture(monkeypatch, tmp_path)
    resolution = registry.BeadHiveResolution("widget-1", None, (first, second) if ambiguous else ())
    monkeypatch.setattr(registry, "resolve_bead_hive", lambda *_args, **_kwargs: resolution)
    monkeypatch.setattr(
        work,
        "_claim_single_bead",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 1
    assert "stage=bead" in result.output
    if ambiguous:
        assert "github/acme/one" in result.output
        assert "github/acme/two" in result.output
        assert "--hive" in result.output
    else:
        assert "not found" in result.output


def test_launch_rejects_unsupported_kind_before_integration_or_claim(tmp_path, monkeypatch):
    _launch_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(herdr_plugin, "supported_kinds", lambda: ["claude", "codex"])
    monkeypatch.setattr(
        herdr_plugin,
        "_integration_ready",
        lambda _kind: (_ for _ in ()).throw(AssertionError("must stop before integration")),
    )
    monkeypatch.setattr(
        work,
        "_claim_single_bead",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not claim")),
    )

    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1", "--kind", "future"])

    assert result.exit_code == 1
    assert "stage=kind" in result.output
    assert "supported kinds: claude, codex" in result.output


def test_launch_startup_failure_closes_created_pane_and_keeps_native_resources(
    tmp_path, monkeypatch
):
    _entry, claim = _launch_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin, "_workspace", lambda *_args: ("w1", "w1:p1"))
    closed = []
    monkeypatch.setattr(herdr_plugin, "_close_pane", closed.append)

    def command(*args, **_kwargs):
        if args[:2] == ("pane", "split"):
            return _result(stdout="w1:p2")
        if args[:2] == ("agent", "start"):
            return _result(1, stderr="integration hook did not report readiness")
        raise AssertionError(args)

    monkeypatch.setattr(herdr_plugin, "_command", command)
    result = runner.invoke(app, ["plugin", "herdr", "launch", "widget-1"])

    assert result.exit_code == 1
    assert "stage=startup" in result.output
    assert str(claim.worktree) in result.output
    assert closed == ["w1:p2"]
