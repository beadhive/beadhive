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

from beadhive import guard, herdr_plugin, herdr_views, plugins, registry, work
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
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result([])
        if argv in (["herdr", "--session", "default", "status"], ["herdr", "status"]):
            return _result(stdout="server ready")
        if argv == ["herdr", "integration", "status"]:
            return _result(stdout="claude: installed\ncodex: absent")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "status"])

    assert result.exit_code == 0, result.output
    assert "herdr: server=up" in result.output
    assert "claude: installed" in result.output
    assert calls == [
        ["herdr", "plugin", "list", "--json"],
        ["herdr", "--session", "default", "status"],
        ["herdr", "integration", "status"],
    ]


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


def _local_package(tmp_path: Path, *, plugin_id: str = "beadhive.herdr") -> Path:
    root = tmp_path / "herdr-plugin"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "herdr-plugin.toml").write_text(
        f'id = "{plugin_id}"\n'
        'name = "Beadhive"\n'
        'version = "0.1.0"\n'
        'platforms = ["linux", "macos"]\n'
    )
    return root


def _registry_result(rows: list[dict]) -> SimpleNamespace:
    return _result(stdout=json.dumps({"id": "cli:plugin", "result": {"plugins": rows}}))


def _allow_local_checkout(monkeypatch) -> None:
    monkeypatch.setattr(herdr_plugin, "_is_git_checkout", lambda _root: True)


def test_register_links_local_package_idempotently_across_invocations(tmp_path, monkeypatch):
    root = _local_package(tmp_path)
    _allow_local_checkout(monkeypatch)
    rows: list[dict] = []
    mutations: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result(rows)
        if argv == ["herdr", "plugin", "link", str(root.resolve()), "--enabled"]:
            mutations.append(argv)
            rows.append(
                {
                    "plugin_id": "beadhive.herdr",
                    "plugin_root": str(root.resolve()),
                    "source": {"kind": "local"},
                    "enabled": True,
                    "version": "0.1.0",
                }
            )
            return _result(stdout="linked")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    first = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])
    restarted = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])

    assert first.exit_code == 0, first.output
    assert "disposition=linked" in first.output
    assert restarted.exit_code == 0, restarted.output
    assert "disposition=already_current" in restarted.output
    assert len(mutations) == 1


def test_register_managed_source_requires_consent_and_pins_ref(monkeypatch):
    rows: list[dict] = []
    mutations: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result(rows)
        expected = [
            "herdr",
            "plugin",
            "install",
            "beadhive/herdr-plugin",
            "--ref",
            "v0.1.0",
            "--yes",
        ]
        assert argv == expected
        mutations.append(argv)
        rows.append(
            {
                "plugin_id": "beadhive.herdr",
                "source": {
                    "kind": "github",
                    "owner": "beadhive",
                    "repo": "herdr-plugin",
                    "requested_ref": "v0.1.0",
                },
                "enabled": True,
            }
        )
        return _result(stdout="installed")

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    refused = runner.invoke(app, ["plugin", "herdr", "add", "--managed-ref", "v0.1.0"])
    installed = runner.invoke(
        app,
        ["plugin", "herdr", "add", "--managed-ref", "v0.1.0", "--yes"],
    )

    assert refused.exit_code == 2
    assert "consent_required" in refused.output
    assert installed.exit_code == 0, installed.output
    assert "repository=beadhive/herdr-plugin ref=v0.1.0" in installed.output
    assert len(mutations) == 1


def test_register_refuses_source_conflict_without_mutation(tmp_path, monkeypatch):
    root = _local_package(tmp_path)
    _allow_local_checkout(monkeypatch)
    rows = [
        {
            "plugin_id": "beadhive.herdr",
            "source": {
                "kind": "github",
                "owner": "beadhive",
                "repo": "herdr-plugin",
                "requested_ref": "v0.1.0",
            },
        }
    ]
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["herdr", "plugin", "list", "--json"]
        return _registry_result(rows)

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])

    assert result.exit_code == 1
    assert "source_conflict" in result.output
    assert "uninstall or unlink" in result.output
    assert calls == [["herdr", "plugin", "list", "--json"]]


def test_register_dry_run_validates_but_does_not_mutate(tmp_path, monkeypatch):
    root = _local_package(tmp_path)
    _allow_local_checkout(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _registry_result([])

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "disposition=planned" in result.output
    assert calls == [["herdr", "plugin", "list", "--json"]]


def test_register_json_uses_canonical_add_command_and_registration_operation(tmp_path, monkeypatch):
    root = _local_package(tmp_path)
    _allow_local_checkout(monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", lambda *a, **k: _registry_result([]))

    result = runner.invoke(
        app,
        ["plugin", "herdr", "add", "--local", str(root), "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["command"] == "plugin herdr add"
    assert payload["operation"] == "register"
    jsonschema.validate(payload, json.loads(_LIFECYCLE_SCHEMA_PATH.read_text()))


def test_register_fences_missing_cli_and_incompatible_manifest(tmp_path, monkeypatch):
    root = _local_package(tmp_path, plugin_id="foreign.plugin")
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: None)
    missing = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])
    assert missing.exit_code == 1
    assert "herdr_cli_unavailable" in missing.output

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    _allow_local_checkout(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("invalid manifest must be refused before Herdr runs")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    incompatible = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])
    assert incompatible.exit_code == 2
    assert "manifest_incompatible" in incompatible.output


def test_register_reports_partial_installation_when_registry_does_not_persist(
    tmp_path, monkeypatch
):
    root = _local_package(tmp_path)
    _allow_local_checkout(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result([])
        return _result(stdout="claimed success")

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "add", "--local", str(root)])

    assert result.exit_code == 1
    assert "partial_installation" in result.output
    assert calls.count(["herdr", "plugin", "list", "--json"]) == 2


def test_status_separates_adapter_package_server_and_agent_integrations(monkeypatch):
    row = {
        "plugin_id": "beadhive.herdr",
        "plugin_root": "/workspace/github/beadhive/herdr-plugin",
        "source": {"kind": "local"},
        "enabled": True,
        "version": "0.1.0",
    }
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result([row])
        if argv == ["herdr", "--session", "default", "status"]:
            return _result(stdout="server ready")
        if argv == ["herdr", "integration", "status"]:
            return _result(stdout="codex: installed")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["adapter"]["state"] == "available"
    assert payload["package"]["state"] == "registered"
    assert payload["package"]["plugin_id"] == "beadhive.herdr"
    assert payload["server"]["available"] is True
    assert payload["integrations"] == [{"kind": "codex", "state": "installed"}]
    assert calls == [
        ["herdr", "plugin", "list", "--json"],
        ["herdr", "--session", "default", "status"],
        ["herdr", "integration", "status"],
    ]


def test_readiness_keeps_package_and_agent_integration_separate(monkeypatch):
    monkeypatch.setattr(
        herdr_plugin,
        "_package_status",
        lambda: (
            {"state": "registered", "plugin_id": "beadhive.herdr"},
            "",
        ),
    )
    monkeypatch.setattr(herdr_plugin.config, "herdr_kind", lambda cfg, entry: "codex")
    monkeypatch.setattr(herdr_plugin, "_integration_ready", lambda kind: (True, "installed"))

    state, detail = herdr_plugin._readiness({}, {})
    assert state == "ok"
    assert "adapter=available" in detail
    assert "package=registered plugin_id=beadhive.herdr" in detail
    assert "agent-integration[codex]=installed" in detail

    monkeypatch.setattr(
        herdr_plugin,
        "_package_status",
        lambda: ({"state": "absent", "plugin_id": "beadhive.herdr"}, ""),
    )
    state, detail = herdr_plugin._readiness({}, {})
    assert state == "warn"
    assert "package=absent" in detail


def test_herdr_plugin_onboard_hook_is_explicit_opt_in(monkeypatch, tmp_path):
    root = _local_package(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(herdr_plugin, "_local_package_checkout", lambda: root)
    monkeypatch.setattr(
        herdr_plugin,
        "_converge_package",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "plugin_id": "beadhive.herdr",
                "source": "local",
                "path": str(root),
                "disposition": "linked",
            }
        ),
    )

    assert herdr_plugin.PLUGIN.onboard_requires_opt_in is True
    assert herdr_plugin.PLUGIN.on_onboard is not None
    herdr_plugin.PLUGIN.on_onboard(SimpleNamespace())
    assert calls == [{"local": root}]


def test_session_selection_defaults_and_resolves_current_only_inside_herdr(monkeypatch):
    monkeypatch.delenv("BH_HERDR_SESSION", raising=False)
    assert herdr_plugin._resolve_session(None).name == "default"
    assert herdr_plugin._session_selection("named.team").name == "named.team"

    monkeypatch.setenv("BH_HERDR_SESSION", "operator-team")
    assert herdr_plugin._resolve_session(None).name == "operator-team"
    assert herdr_plugin._resolve_session("explicit-team").name == "explicit-team"
    monkeypatch.setenv("BH_HERDR_SESSION", "not a session")
    with pytest.raises(ValueError, match="BH_HERDR_SESSION"):
        herdr_plugin._resolve_session(None)
    monkeypatch.delenv("BH_HERDR_SESSION")

    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    with pytest.raises(ValueError, match="Herdr-managed pane"):
        herdr_plugin._session_selection("current")

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    assert herdr_plugin._session_selection("active").name == "default"
    monkeypatch.setenv("HERDR_SESSION", "operator-team")
    selected = herdr_plugin._session_selection("current")
    assert selected.name == "operator-team"
    assert selected.current is True

    with pytest.raises(ValueError, match="Herdr session name"):
        herdr_plugin._session_selection("not a session")


def test_status_current_targets_injected_session_and_reports_it(monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w7:p3")
    monkeypatch.setenv("HERDR_SESSION", "operator-team")
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv == ["herdr", "plugin", "list", "--json"]:
            return _registry_result([])
        if argv == ["herdr", "--session", "operator-team", "status"]:
            return _result(stdout="server ready")
        if argv == ["herdr", "integration", "status"]:
            return _result(stdout="codex: installed")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "status", "--session", "current", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["session"] == "operator-team"
    assert calls == [
        ["herdr", "plugin", "list", "--json"],
        ["herdr", "--session", "operator-team", "status"],
        ["herdr", "integration", "status"],
    ]

    attached = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "attach",
            "bh-widget-1",
            "--session",
            "operator-team",
            "--json",
        ],
    )
    assert attached.exit_code == 0, attached.output
    attach_payload = json.loads(attached.stdout)
    assert attach_payload["session"] == "operator-team"
    assert attach_payload["attach_argv"] == [
        "herdr",
        "--session",
        "operator-team",
        "agent",
        "attach",
        "bh-widget-1",
    ]


def test_session_prepare_creates_absent_exact_session_without_touching_others(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv == ["herdr", "--session", "fresh", "api", "snapshot"]:
            return _result(stdout="{}")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection("fresh"))
    try:
        snapshot, detail = herdr_plugin._prepare_selected_session()
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    assert snapshot == {}
    assert detail == ""
    assert calls == [["herdr", "--session", "fresh", "api", "snapshot"]]


def test_stopped_reserved_session_delete_success_recreates_exact_session(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return _result(1, stderr="session is stopped")
        if len(calls) == 2:
            return _result(
                stdout=json.dumps(
                    {
                        "sessions": [
                            {"name": "bh-supervisor", "running": False},
                        ]
                    }
                )
            )
        if len(calls) == 3:
            return _result(stdout='{"deleted":"bh-supervisor"}')
        if len(calls) == 4:
            return _result(stdout='{"snapshot":{"session":"bh-supervisor"}}')
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection("bh-supervisor"))
    try:
        snapshot, detail = herdr_plugin._prepare_selected_session()
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    assert snapshot == {"session": "bh-supervisor"}
    assert detail == ""
    assert calls == [
        ["herdr", "--session", "bh-supervisor", "api", "snapshot"],
        ["herdr", "session", "list", "--json"],
        ["herdr", "session", "delete", "bh-supervisor", "--json"],
        ["herdr", "--session", "bh-supervisor", "api", "snapshot"],
    ]


def test_stopped_reserved_session_is_recreated_and_accepts_concurrent_winner(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    snapshots = iter([None, {}])
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        herdr_plugin,
        "_session_states",
        lambda: ({"bh-supervisor": herdr_plugin._SessionState("bh-supervisor", False)}, ""),
    )
    deletes = []
    monkeypatch.setattr(
        herdr_plugin,
        "_invoke",
        lambda argv, **_kwargs: deletes.append(argv) or _result(1, stderr="already deleted"),
    )

    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection("bh-supervisor"))
    try:
        snapshot, detail = herdr_plugin._prepare_selected_session()
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    assert snapshot == {}
    assert detail == ""
    assert deletes == [["herdr", "session", "delete", "bh-supervisor", "--json"]]


@pytest.mark.parametrize("session", ["default", "team"])
def test_stopped_operator_owned_session_is_never_deleted(monkeypatch, session):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: None)
    monkeypatch.setattr(
        herdr_plugin,
        "_session_states",
        lambda: ({session: herdr_plugin._SessionState(session, False)}, ""),
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not delete")),
    )
    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection(session))
    try:
        snapshot, detail = herdr_plugin._prepare_selected_session()
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    assert snapshot is None
    assert f"session '{session}' is stopped" in detail
    assert f"herdr session delete {session}" in detail


def test_running_named_session_with_unavailable_snapshot_is_never_seized(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: None)
    monkeypatch.setattr(
        herdr_plugin,
        "_session_states",
        lambda: ({"foreign": herdr_plugin._SessionState("foreign", True)}, ""),
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not mutate")),
    )
    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection("foreign"))
    try:
        snapshot, detail = herdr_plugin._prepare_selected_session()
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    assert snapshot is None
    assert detail == "session 'foreign' is running but its snapshot is unavailable"


def test_incompatible_session_inventory_refuses_recovery(monkeypatch):
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: None)
    monkeypatch.setattr(
        herdr_plugin,
        "_session_states",
        lambda: (None, "session list included an incompatible record"),
    )

    snapshot, detail = herdr_plugin._prepare_selected_session()

    assert snapshot is None
    assert "could not inspect Herdr sessions" in detail
    assert "incompatible record" in detail


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
        ["herdr", "--session", "default", "agent", "list", "--json"],
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
    monkeypatch.setattr(
        herdr_plugin.bd,
        "show",
        lambda *_args, **_kwargs: {
            "id": "bh-1",
            "status": "in_progress",
            "assignee": "dev/test",
            "labels": [],
        },
    )
    monkeypatch.setattr(
        herdr_plugin.worktree, "current_branch", lambda _target: "wt/bead/issue/bh-1"
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_spawn_target_proof",
        lambda _target, workspace, pane: herdr_plugin._SpawnTargetProof(
            workspace, pane, "idle", "sha256:" + "1" * 64
        ),
    )
    return target


def _owned_dispatch_target(tmp_path, monkeypatch):
    target = "bh-bh-7"
    bead = "bh-7"
    hive = "github/acme/widgets"
    cwd = tmp_path / bead
    cwd.mkdir(exist_ok=True)
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = {
        "agents": [{"name": target, "state": "idle", "pane_id": "w1:p2"}],
        "panes": [
            {
                "pane_id": "w1:p2",
                "workspace_id": "w1",
                "label": target,
                "cwd": str(cwd),
                "tokens": {
                    "bh_owner": "bh.plugin.herdr/v1",
                    "bh_hive_id": hive,
                    "bh_bead_id": bead,
                    "bh_target": target,
                    "bh_schema": "1",
                },
            }
        ],
        "workspaces": [{"workspace_id": "w1", "label": f"bh:{hive}"}],
    }
    branch = f"wt/bead/issue/{bead}"
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        herdr_plugin,
        "_managed_worktree_location",
        lambda *_args, **_kwargs: ({}, tmp_path, cwd, branch),
    )
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "managed",
        lambda _cfg: [("bh", str(cwd), branch)],
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
        "default",
        "agent",
        "start",
        "bh-bh-1",
        "--kind",
        "codex",
        "--pane",
        "w1:p2",
    ] in calls
    assert ["herdr", "--session", "default", "pane", "rename", "w1:p2", "bh-bh-1"] in calls
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
        "default",
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
    assert ["herdr", "--session", "default", "pane", "close", "w1:p2", "--no-focus"] in calls


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
    assert ["herdr", "--session", "default", "pane", "close", "w1:p2", "--no-focus"] in calls


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
    assert ["herdr", "--session", "default", "pane", "close", "w1:p2", "--no-focus"] in calls


@pytest.mark.parametrize(("kind", "state"), [("codex", "done"), ("claude", "blocked")])
def test_spawn_refuses_terminal_or_blocked_startup_and_closes_exact_pane(
    tmp_path, monkeypatch, kind, state
):
    worktree_path = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(
        herdr_plugin,
        "_spawn_target_proof",
        lambda *_args: (_ for _ in ()).throw(
            herdr_plugin._SpawnReadinessError(
                f"startup settled in lifecycle state {state!r}", lifecycle_state=state
            )
        ),
    )
    calls = []

    def fake_run(argv, **_kwargs):
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
            return _result(stdout=f"assistant: {herdr_plugin._WARMUP_TOKEN}")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "spawn",
            "--hive",
            "h",
            "--bead",
            "bh-1",
            "--kind",
            kind,
            "--json",
        ],
    )

    assert result.exit_code == 1
    receipt = json.loads(result.stdout)
    _assert_lifecycle_receipt(receipt, "spawn", "failed")
    assert receipt["error"]["code"] == "spawn_not_dispatchable"
    assert receipt["resulting_state"] == state
    assert receipt["pane"] == "w1:p2"
    assert receipt["cleanup"] == {"attempted": True, "succeeded": True, "detail": ""}
    assert receipt["retained_resources"] == [{"kind": "worktree", "path": str(worktree_path)}]
    assert ["herdr", "--session", "default", "pane", "close", "w1:p2", "--no-focus"] in calls


def test_spawn_cleanup_failure_retains_exact_pane_receipt(tmp_path, monkeypatch):
    worktree_path = _spawn_worktree(tmp_path, monkeypatch)
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    monkeypatch.setattr(
        herdr_plugin,
        "_spawn_target_proof",
        lambda *_args: (_ for _ in ()).throw(
            herdr_plugin._SpawnReadinessError(
                "startup settled in lifecycle state 'blocked'", lifecycle_state="blocked"
            )
        ),
    )

    def fake_run(argv, **_kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if argv[-2:] == ["api", "snapshot"]:
            return _result(stdout="{}")
        if "workspace" in argv and "create" in argv:
            return _result(stdout="w1")
        if "split" in argv:
            return _result(stdout="w1:p2")
        if "read" in argv:
            return _result(stdout=f"assistant: {herdr_plugin._WARMUP_TOKEN}")
        if "close" in argv:
            return _result(1, stderr="pane remained busy")
        return _result()

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "spawn",
            "--hive",
            "h",
            "--bead",
            "bh-1",
            "--kind",
            "claude",
            "--json",
        ],
    )

    assert result.exit_code == 1
    receipt = json.loads(result.stdout)
    assert receipt["cleanup"] == {
        "attempted": True,
        "succeeded": False,
        "detail": "pane remained busy",
    }
    assert receipt["retained_resources"] == [
        {"kind": "worktree", "path": str(worktree_path)},
        {"kind": "pane", "id": "w1:p2", "target": "bh-bh-1"},
    ]


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


def test_dispatch_verifies_a_new_prompt_landed_for_claude_and_codex(tmp_path, monkeypatch):
    """Both supported harnesses pass only when the post-read adds the real turn."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)

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
        result = runner.invoke(app, ["plugin", "herdr", "dispatch", target, prompt])

        assert result.exit_code == 0, result.output
        assert [
            "herdr",
            "--session",
            "default",
            "agent",
            "prompt",
            target,
            prompt,
            "--wait",
            "--timeout",
            "60000",
        ] in calls
        assert sum("read" in call and "visible" in call for call in calls) == 2


def test_dispatch_rejects_stale_prompt_from_a_prior_turn(tmp_path, monkeypatch):
    """An unchanged pane containing an older identical prompt is not a delivery proof."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
    prompt = "implement the requested change"
    reads = iter([f"user: {prompt}\\nassistant: done", f"user: {prompt}\\nassistant: done"])

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: done")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", target, prompt])

    assert result.exit_code == 1
    assert "did not reach a new real agent turn" in result.output


def test_dispatch_accepts_a_repeated_prompt_only_when_a_new_copy_appears(tmp_path, monkeypatch):
    """Repeating an intentional prompt remains valid, but needs a new pane occurrence."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
    prompt = "repeat this prompt"
    reads = iter([f"user: {prompt}", f"user: {prompt}\\nassistant: done\\nuser: {prompt}"])

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: done")

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", target, prompt])

    assert result.exit_code == 0, result.output


def test_dispatch_fails_when_codex_first_run_screen_drops_prompt(tmp_path, monkeypatch):
    """Codex can say done after its hook-review screen consumed the first prompt."""
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
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
    result = runner.invoke(app, ["plugin", "herdr", "dispatch", target, prompt])

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
    assert payload["session"] == "default"
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


def test_lifecycle_status_ps_and_attach_emit_shared_json_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    cwd = tmp_path / "bh-7"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = _roster_snapshot("bh-bh-7", cwd, bead="bh-7")
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "bh-7")
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)

    def fake_run(argv, **kwargs):
        if argv in (["herdr", "--session", "default", "status"], ["herdr", "status"]):
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
    assert ps_payload["agents"][0]["target"] == "bh-bh-7"
    assert ps_payload["agents"][0]["hive"] == "github/acme/widgets"
    assert ps_payload["agents"][0]["bead"] == "bh-7"
    assert ps_payload["agents"][0]["ownership"]["state"] == "owned"
    attach_payload = json.loads(attach.stdout)
    _assert_lifecycle_receipt(attach_payload, "attach", "instructions")
    assert attach_payload["attach_argv"] == [
        "herdr",
        "--session",
        "default",
        "agent",
        "attach",
        "bh-bh-7",
    ]


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_dispatch_safe_input_preserves_adversarial_multiline_without_leakage(
    source, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
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
    argv = ["plugin", "herdr", "dispatch", target, "--json"]
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
    assert socket_prompts == [(target, secret, {})]
    assert all(secret not in "\0".join(call) for call in process_argv)
    assert secret not in result.output
    assert secret not in caplog.text


def test_safe_dispatch_accepts_maximum_prompt_when_socket_acknowledges_delivery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
    prompt = "x" * (1024 * 1024)
    prompt_path = tmp_path / "maximum-prompt.txt"
    prompt_path.write_text(prompt)

    def fake_run(argv, **kwargs):
        if argv == ["herdr", "status"]:
            return _result()
        if "read" in argv:
            return _result(stdout="agent: idle")
        raise AssertionError(argv)

    monkeypatch.setattr(herdr_plugin.run, "run", fake_run)
    monkeypatch.setattr(
        herdr_plugin,
        "_prompt_over_socket",
        lambda target, value: _result(
            stdout='{"result":{"type":"agent_prompt","agent_status":"done"}}'
        ),
    )

    result = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "dispatch",
            target,
            "--prompt-file",
            str(prompt_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["disposition"] == "dispatched"
    assert payload["delivery_verified"] is True
    assert prompt not in result.stdout


def test_prompt_file_enforces_limit_during_read(monkeypatch):
    read_sizes = []

    class FakeFile:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, size=-1):
            read_sizes.append(size)
            if size == -1:
                return "x" * (herdr_plugin._MAX_PROMPT_BYTES + 1)
            return b"x" * size

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: FakeFile())

    with pytest.raises(ValueError, match="exceeds"):
        herdr_plugin._prompt_input(None, from_stdin=False, prompt_file="/fake/prompt")

    assert read_sizes == [herdr_plugin._MAX_PROMPT_BYTES + 1]


def test_prompt_stdin_enforces_limit_during_binary_read(monkeypatch):
    read_sizes = []

    class FakeBuffer:
        def read(self, size=-1):
            read_sizes.append(size)
            return b"x" * size

    class FakeStdin:
        buffer = FakeBuffer()

        def read(self, _size=-1):
            raise AssertionError("bounded prompt input must use the binary stdin stream")

    monkeypatch.setattr(herdr_plugin.sys, "stdin", FakeStdin())

    with pytest.raises(ValueError, match="exceeds"):
        herdr_plugin._prompt_input(None, from_stdin=True, prompt_file="")

    assert read_sizes == [herdr_plugin._MAX_PROMPT_BYTES + 1]


def test_prompt_file_rejects_invalid_utf8(tmp_path):
    prompt_path = tmp_path / "invalid-prompt.txt"
    prompt_path.write_bytes(b"valid prefix\xffinvalid suffix")

    with pytest.raises(ValueError, match="prompt input must be valid UTF-8"):
        herdr_plugin._prompt_input(None, from_stdin=False, prompt_file=str(prompt_path))


def test_dispatch_json_refusal_is_structured_and_not_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(herdr_plugin.shutil, "which", lambda _name: "/usr/bin/herdr")
    target = _owned_dispatch_target(tmp_path, monkeypatch)
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
        ["plugin", "herdr", "dispatch", target, prompt, "--json"],
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
    monkeypatch.setattr(
        herdr_plugin,
        "_owned_live_pane_proof",
        lambda _target: ("w1:p2", "sha256:" + "1" * 64),
    )
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: {})
    monkeypatch.setattr(
        herdr_plugin,
        "_spawn_target_proof",
        lambda _target, workspace, pane: herdr_plugin._SpawnTargetProof(
            workspace, pane, "idle", "sha256:" + "2" * 64
        ),
    )

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
    assert reap_payload["source_revision"] == "sha256:" + "1" * 64


def test_safe_prompt_socket_request_keeps_body_out_of_process_metadata(monkeypatch):
    secret = "multiline\nsecret $(not-a-shell)\nend"
    sent = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            request_id = json.loads(sent[0])["id"]
            return (
                json.dumps(
                    {
                        "id": request_id,
                        "result": {"type": "agent_prompt", "agent_status": "done"},
                    }
                ).encode()
                + b"\n"
            )

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


def test_safe_prompt_socket_refuses_mismatched_response_id(monkeypatch):
    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return b'{"id":"other","result":{"type":"agent_prompt","agent_status":"done"}}\n'

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _value):
            pass

        def makefile(self, _mode):
            return FakeStream()

    monkeypatch.setattr(herdr_plugin, "_session_socket_path", lambda: (Path("/tmp/herdr.sock"), ""))
    monkeypatch.setattr(herdr_plugin.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(herdr_plugin.uuid, "uuid4", lambda: SimpleNamespace(hex="expected"))

    result = herdr_plugin._prompt_over_socket("bh-bh-7", "secret")

    assert result.returncode == 1
    assert result.stderr == "Herdr returned a mismatched agent.prompt response"


def test_safe_prompt_socket_refuses_unexpected_result_semantics(monkeypatch):
    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return (
                b'{"id":"bh_prompt_expected","result":'
                b'{"type":"workspace_created","agent_status":"done"}}\n'
            )

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _value):
            pass

        def makefile(self, _mode):
            return FakeStream()

    monkeypatch.setattr(herdr_plugin, "_session_socket_path", lambda: (Path("/tmp/x"), ""))
    monkeypatch.setattr(herdr_plugin.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(herdr_plugin.uuid, "uuid4", lambda: SimpleNamespace(hex="expected"))

    result = herdr_plugin._prompt_over_socket("bh-bh-7", "secret")

    assert result.returncode == 1
    assert result.stderr == "Herdr returned an invalid agent.prompt response"


def test_safe_prompt_socket_redacts_server_error_messages(monkeypatch):
    secret = "raw server echoed this secret"

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return (
                json.dumps(
                    {
                        "id": "bh_prompt_expected",
                        "error": {"code": "refused", "message": secret},
                    }
                ).encode()
                + b"\n"
            )

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _value):
            pass

        def makefile(self, _mode):
            return FakeStream()

    monkeypatch.setattr(herdr_plugin, "_session_socket_path", lambda: (Path("/tmp/x"), ""))
    monkeypatch.setattr(herdr_plugin.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(herdr_plugin.uuid, "uuid4", lambda: SimpleNamespace(hex="expected"))

    result = herdr_plugin._prompt_over_socket("bh-bh-7", secret)

    assert result.returncode == 1
    assert result.stderr == "Herdr refused the agent.prompt request"
    assert secret not in result.stderr


def test_lifecycle_receipt_schema_covers_every_json_command():
    schema = json.loads(_LIFECYCLE_SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert set(schema["properties"]["operation"]["enum"]) == {
        "status",
        "register",
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
    assert result.output.strip() == "herdr --session default agent attach bh-bh-1"


def test_attach_uses_environment_then_explicit_session_without_mutation(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("attach must not inspect or alter herdr state")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    monkeypatch.setenv("BH_HERDR_SESSION", "team")
    environment = runner.invoke(app, ["plugin", "herdr", "attach", "bh-bh-1"])
    explicit = runner.invoke(app, ["plugin", "herdr", "attach", "bh-bh-1", "--session", "other"])

    assert environment.exit_code == explicit.exit_code == 0
    assert environment.output.strip() == "herdr --session team agent attach bh-bh-1"
    assert explicit.output.strip() == "herdr --session other agent attach bh-bh-1"


@pytest.mark.parametrize(
    ("environment", "arguments", "source"),
    [
        ("not a session", [], "BH_HERDR_SESSION"),
        ("team", ["--session", "not a session"], "--session"),
    ],
)
def test_invalid_session_source_refuses_before_attach_action(
    monkeypatch, environment, arguments, source
):
    def boom(*args, **kwargs):
        raise AssertionError("invalid selection must fail before any Herdr action")

    monkeypatch.setattr(herdr_plugin.run, "run", boom)
    monkeypatch.setenv("BH_HERDR_SESSION", environment)
    result = runner.invoke(app, ["plugin", "herdr", "attach", "bh-bh-1", *arguments])

    assert result.exit_code == 2
    assert source in result.output


def test_reap_refuses_reserved_name_without_current_ownership_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin.worktree, "managed", lambda _cfg: [])
    monkeypatch.setattr(
        herdr_plugin,
        "_session_snapshot",
        lambda: {
            "agents": [{"name": "bh-bh-1", "state": "idle", "pane_id": "w1:p2"}],
            "panes": [
                {
                    "pane_id": "w1:p2",
                    "workspace_id": "w1",
                    "label": "bh-bh-1",
                    "cwd": "/tmp/manual",
                }
            ],
            "workspaces": [{"workspace_id": "w1", "label": "manual"}],
        },
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_command",
        lambda *args, **_kwargs: calls.append(args) or _result(),
    )
    result = runner.invoke(app, ["plugin", "herdr", "reap", "bh-bh-1"])

    assert result.exit_code == 1
    assert "refusing unmanaged" in result.output
    assert not any(call[:2] == ("pane", "close") for call in calls)


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


def test_reap_accepts_owned_metadata_on_an_agent_wrapper(tmp_path, monkeypatch):
    target = "bh-bh-1"
    bead = "bh-1"
    hive = "github/acme/widgets"
    cwd = tmp_path / bead
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    branch = f"wt/bead/issue/{bead}"
    calls = []
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(
        herdr_plugin,
        "_managed_worktree_location",
        lambda *_args, **_kwargs: ({}, tmp_path, cwd, branch),
    )
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "managed",
        lambda _cfg: [("bh", str(cwd), branch)],
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_session_snapshot",
        lambda: {
            "agents": [
                {
                    "agent": {"name": target, "state": "idle"},
                    "workspace_id": "w1",
                    "workspace_label": f"bh:{hive}",
                    "pane": {
                        "id": "w1:p2",
                        "label": target,
                        "cwd": str(cwd),
                        "tokens": {
                            "bh_owner": "bh.plugin.herdr/v1",
                            "bh_hive_id": hive,
                            "bh_bead_id": bead,
                            "bh_target": target,
                            "bh_schema": "1",
                        },
                    },
                }
            ],
            "workspaces": [{"workspace_id": "w1", "label": f"bh:{hive}"}],
        },
    )
    monkeypatch.setattr(
        herdr_plugin,
        "_command",
        lambda *args, **_kwargs: calls.append(args) or _result(),
    )
    result = runner.invoke(app, ["plugin", "herdr", "reap", target])

    assert result.exit_code == 0, result.output
    assert ("pane", "close", "w1:p2", "--no-focus") in calls


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
                "default",
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
        "session": "default",
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
    pane_metadata = next(call for call in calls if call[:2] == ("pane", "report-metadata"))
    assert "bh_owner=bh.plugin.herdr/v1" in pane_metadata
    assert "bh_hive_id=github/acme/widgets" in pane_metadata
    assert "bh_bead_id=widget-1" in pane_metadata
    assert "bh_target=bh-widget-1" in pane_metadata
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
    result = runner.invoke(
        app, ["plugin", "herdr", "launch", "widget-1", "--session", "operator-team"]
    )

    assert result.exit_code == 0, result.output
    assert "disposition=reused" in result.output
    assert "session=operator-team" in result.output
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


def _roster_snapshot(
    target, cwd, *, hive="github/acme/widgets", bead="widget-1", tokens=True, state="idle"
):
    pane = {
        "pane_id": "w1:p2",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "label": target,
        "cwd": str(cwd),
    }
    if tokens:
        pane["tokens"] = {
            "bh_owner": "bh.plugin.herdr/v1",
            "bh_hive_id": hive,
            "bh_bead_id": bead,
            "bh_target": target,
            "bh_schema": "1",
        }
    return {
        "agents": [
            {
                "name": target,
                "state": state,
                "pane_id": "w1:p2",
                "created_at": "2026-08-27T12:00:00Z",
                "last_activity_at": "2026-08-27T12:01:00Z",
            }
        ],
        "panes": [pane],
        "workspaces": [{"workspace_id": "w1", "label": f"bh:{hive}"}],
    }


def test_collapsed_batch_spawn_ps_dispatch_and_cleanup_keep_child_identity_and_session(
    tmp_path, monkeypatch
):
    """The Herdr lifecycle follows one claimed group worktree without minting a child branch."""
    child = "widget-1.2"
    group = "widget-1"
    canonical_hive = "github/acme/widgets"
    branch = f"wt/batch/{group}"
    batch = tmp_path / f"batch-{group}"
    batch.mkdir()
    (batch / ".git").write_text("gitdir: elsewhere\n")
    dedicated = tmp_path / child
    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    cfg = {"managed_repos": [entry]}
    issue = {
        "id": child,
        "status": "in_progress",
        "assignee": "dev/group",
        "labels": [f"batch:{group}"],
    }

    def locate(_cfg, hive, bead="", branch="", **_kwargs):
        assert hive in {"widgets", canonical_hive}
        if branch:
            assert branch == f"batch/{group}"
            return entry, tmp_path, batch, f"wt/{branch}"
        assert bead == child
        return entry, tmp_path, dedicated, f"wt/bead/issue/{child}"

    monkeypatch.setattr(herdr_plugin.config, "load", lambda: cfg)
    monkeypatch.setattr(herdr_plugin.worktree, "locate", locate)
    monkeypatch.setattr(herdr_plugin.worktree, "current_branch", lambda _path: branch)
    monkeypatch.setattr(
        herdr_plugin.worktree, "managed", lambda _cfg: [("widget", str(batch), branch)]
    )
    monkeypatch.setattr(herdr_plugin.bd, "show", lambda *_args, **_kwargs: issue)
    monkeypatch.setattr(herdr_plugin, "_prepare_selected_session", lambda: ({}, ""))
    monkeypatch.setattr(herdr_plugin, "_strict_live_target", lambda *_args: None)
    monkeypatch.setattr(herdr_plugin, "_resolve_kind", lambda kind, *_args: kind)
    target = herdr_plugin._launch_target(child)
    snapshot = _roster_snapshot(target, batch, hive=canonical_hive, bead=child)
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    workspaces = []

    def workspace(hive, cwd):
        workspaces.append((hive, cwd))
        return "w1", "w1:p1"

    monkeypatch.setattr(herdr_plugin, "_workspace", workspace)

    def spawn_command(*args, **_kwargs):
        if args[:2] == ("pane", "split"):
            assert str(batch) in args
            return _result(stdout='{"pane":{"pane_id":"w1:p2"}}')
        if args[:2] == ("agent", "read"):
            return _result(stdout=f"assistant: {herdr_plugin._WARMUP_TOKEN}")
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", spawn_command)
    spawned = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "spawn",
            "--hive",
            "widgets",
            "--bead",
            child,
            "--kind",
            "codex",
            "--session",
            "batch-session",
            "--json",
        ],
    )

    assert spawned.exit_code == 0, spawned.output
    receipt = json.loads(spawned.stdout)
    assert receipt["bead"] == child
    assert receipt["hive"] == "widgets"  # echoes the exact --hive requested, not the canonical form
    assert receipt["session"] == "batch-session"
    assert receipt["worktree"] == str(batch)
    assert not dedicated.exists()
    assert workspaces == [(canonical_hive, batch)]

    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    listed = runner.invoke(app, ["plugin", "herdr", "ps", "--session", "batch-session", "--json"])
    assert listed.exit_code == 0, listed.output
    agent = json.loads(listed.stdout)["agents"][0]
    assert agent["bead"] == child
    assert agent["presentation"]["session"] == "batch-session"
    assert agent["worktree"] == {"path": str(batch), "state": "available", "branch": branch}
    assert agent["ownership"]["state"] == "owned"

    reads = iter(["agent: idle", "user: continue\nassistant: working"])
    calls = []

    def lifecycle_command(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("agent", "read"):
            return _result(stdout=next(reads))
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", lifecycle_command)
    dispatched = runner.invoke(
        app,
        [
            "plugin",
            "herdr",
            "dispatch",
            target,
            "continue",
            "--session",
            "batch-session",
            "--json",
        ],
    )
    reaped = runner.invoke(
        app,
        ["plugin", "herdr", "reap", target, "--session", "batch-session", "--json"],
    )
    assert dispatched.exit_code == 0, dispatched.output
    assert json.loads(dispatched.stdout)["session"] == "batch-session"
    assert reaped.exit_code == 0, reaped.output
    assert json.loads(reaped.stdout)["session"] == "batch-session"
    assert ("pane", "close", "w1:p2", "--no-focus") in calls


@pytest.mark.parametrize(
    ("issue", "make_batch", "requested_hive"),
    [
        (
            {"id": "widget-1.2", "status": "open", "assignee": "", "labels": ["batch:g"]},
            True,
            "github/acme/widgets",
        ),
        (
            {
                "id": "widget-1.2",
                "status": "in_progress",
                "assignee": "dev/group",
                "labels": ["batch:g", "batch:other"],
            },
            True,
            "github/acme/widgets",
        ),
        (
            {
                "id": "widget-1.2",
                "status": "in_progress",
                "assignee": "dev/group",
                "labels": ["batch:g", "review:pending"],
            },
            False,
            "github/acme/widgets",
        ),
        (None, True, "github/acme/other"),
    ],
    ids=("unclaimed", "ambiguous", "submitted_without_worktree", "cross_hive"),
)
def test_batch_spawn_refuses_unproven_worktree_matches(
    tmp_path, monkeypatch, issue, make_batch, requested_hive
):
    child = "widget-1.2"
    batch = tmp_path / "batch-g"
    if make_batch:
        batch.mkdir()
        (batch / ".git").write_text("gitdir: elsewhere\n")

    def locate(_cfg, _hive, bead="", branch="", **_kwargs):
        target = batch if branch else tmp_path / child
        resolved_branch = f"wt/{branch}" if branch else f"wt/bead/issue/{bead}"
        return {}, tmp_path, target, resolved_branch

    monkeypatch.setattr(herdr_plugin.worktree, "locate", locate)
    monkeypatch.setattr(herdr_plugin.worktree, "current_branch", lambda _path: "wt/batch/g")
    monkeypatch.setattr(herdr_plugin.bd, "show", lambda *_args, **_kwargs: issue)

    with pytest.raises(typer.Exit):
        herdr_plugin._managed_worktree(requested_hive, child, {})


def _mock_roster_worktree(monkeypatch, root, cwd, bead):
    branch = f"wt/bead/issue/{bead}"
    monkeypatch.setattr(
        herdr_plugin,
        "_managed_worktree_location",
        lambda *_args, **_kwargs: ({}, root, cwd, branch),
    )
    monkeypatch.setattr(
        herdr_plugin.worktree,
        "managed",
        lambda _cfg: [("widget", str(cwd), branch)] if cwd.is_dir() else [],
    )


def test_roster_correlates_encoded_target_from_explicit_metadata(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1.2"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    bead = "widget-1.2"
    target = herdr_plugin._launch_target(bead)
    assert target != f"bh-{bead}"
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, bead)

    payload = herdr_plugin._roster_payload(
        _roster_snapshot(target, cwd, bead=bead), {"managed_repos": []}
    )
    agent = payload["agents"][0]

    assert payload["schema_version"] == 1
    assert payload["revision"].startswith("sha256:")
    assert payload["session"] == "default"
    assert payload["authoritative_session"] is True
    assert agent["target"] == target
    assert agent["revision"].startswith("sha256:")
    assert agent["bead"] == bead
    assert agent["ownership"]["state"] == "owned"
    assert agent["ownership"]["association"] == "metadata"
    assert agent["presentation"] == {
        "session": "default",
        "workspace": "w1",
        "workspace_label": "bh:github/acme/widgets",
        "tab": "w1:t1",
        "pane": "w1:p2",
    }
    assert {item["availability"] for item in agent["capabilities"].values()} == {"allowed"}
    actions = {item["id"]: item for item in agent["advertised_actions"]}
    assert actions["agent.dispatch"]["availability"] == "allowed"
    assert actions["agent.dispatch"]["input"]["transport"] == "stdin"
    assert actions["agent.dispatch"]["input"]["schema"]["sensitive"] is True
    assert actions["agent.reap"]["availability"] == "confirmation-required"
    assert actions["agent.reap"]["preconditions"]["mustMatch"] is True
    assert actions["agent.reap"]["sourceRevision"] == payload["revision"]
    assert actions["agent.reap"]["preconditions"]["sourceRevision"] == payload["revision"]


def test_spawn_target_proof_requires_current_idle_dispatch_action(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    target = "bh-widget-1"
    current = [_roster_snapshot(target, cwd)]
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: current[0])

    proof = herdr_plugin._spawn_target_proof(target, "w1", "w1:p2")

    assert proof.workspace == "w1"
    assert proof.pane == "w1:p2"
    assert proof.lifecycle_state == "idle"
    assert proof.source_revision.startswith("sha256:")

    current[0] = _roster_snapshot(target, cwd, state="blocked")
    with pytest.raises(herdr_plugin._SpawnReadinessError, match="state 'blocked', not idle"):
        herdr_plugin._spawn_target_proof(target, "w1", "w1:p2")


@pytest.mark.parametrize(
    ("receipts", "expected"),
    [
        ({"agent": "claude"}, "claude"),
        ({"agent": " Codex ", "kind": "codex"}, "codex"),
        ({}, "unknown"),
        ({"agent": "gemini"}, "unknown"),
        ({"agent": "claude", "kind": "codex"}, "unknown"),
    ],
    ids=("claude", "codex", "missing", "malformed", "conflicting"),
)
def test_roster_preserves_only_one_canonical_live_harness(
    tmp_path, monkeypatch, receipts, expected
):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = _roster_snapshot("bh-widget-1", cwd)
    snapshot["agents"][0].update(receipts)
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    monkeypatch.setattr(herdr_plugin.store_locator, "dolt_mode", lambda _path: "server")
    monkeypatch.setattr(
        herdr_plugin.bd,
        "show",
        lambda *_args, **_kwargs: {
            "labels": ["harness:codex"],
            "assignee": "dev/test",
            "issue_type": "task",
            "status": "in_progress",
        },
    )

    agent = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})["agents"][0]

    assert agent["ownership"]["state"] == "owned"
    assert agent["facts"]["harness"] == expected


def test_live_harness_flows_to_exact_pane_presentation_title(tmp_path, monkeypatch):
    hive = "github/acme/widgets"
    bead = "widget-1"
    cwd = tmp_path / bead
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = _roster_snapshot(f"bh-{bead}", cwd, hive=hive, bead=bead)
    snapshot["agents"][0].update(
        {
            "agent": "claude",
            "agent_session": {
                "agent": "claude",
                "kind": "id",
                "source": "herdr:claude",
                "value": "session-1",
            },
            "beadhive_role": "developer",
            "work_phase": "implement",
            "work_operation": "implement",
            "terminal_phase": False,
        }
    )
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, bead)
    roster = herdr_plugin._roster_payload(snapshot, {"managed_repos": []})
    queues = {
        name: {
            "revision": f"{name}-r1",
            "coverage": {"state": "complete"},
            "items": ([{"id": bead}] if name == "active" else []),
            "warnings": [],
        }
        for name in ("ready", "active", "blocked")
    }

    presentation = herdr_views.presentation_payload(
        hive,
        {
            "canonical_id": hive,
            "prefix": "wdg",
            "provider": "github",
            "organization": "acme",
            "repository": "widgets",
            "affiliation": "maintainer",
        },
        {
            "source_revision": "inventory-r1",
            "coverage": {"state": "complete"},
            "worktrees": [{"hive_id": hive, "bead_id": bead, "worktree_id": f"{hive}:{bead}"}],
            "total": 1,
            "warnings": [],
        },
        queues,
        roster,
        snapshot,
        dolt={
            "sourceRevision": "dolt-r1",
            "ahead": 0,
            "behind": 0,
            "coverage": {"state": "complete"},
        },
        generated_at=10_000,
        sequence=99,
    )

    assert roster["agents"][0]["facts"]["harness"] == "claude"
    assert presentation["panes"][0]["correlation"]["state"] == "exact"
    assert presentation["panes"][0]["report"]["tokens"]["bh_agent_title"] == (
        "[claude] bh-developer"
    )


def test_reap_accepts_encoded_target_when_current_roster_proves_ownership(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1.2"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    bead = "widget-1.2"
    target = herdr_plugin._launch_target(bead)
    snapshot = _roster_snapshot(target, cwd, bead=bead)
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, bead)
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)

    result = runner.invoke(app, ["plugin", "herdr", "reap", target, "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["disposition"] == "reaped"
    assert receipt["source_revision"].startswith("sha256:")
    assert ("pane", "close", "w1:p2", "--no-focus") in calls


@pytest.mark.parametrize("state", ["blocked", "done"])
def test_spawn_receipt_reaps_plugin_owned_blocked_or_terminal_pane(tmp_path, monkeypatch, state):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    target = "bh-widget-1"
    snapshot = _roster_snapshot(target, cwd, state=state)
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)
    result = runner.invoke(app, ["plugin", "herdr", "reap", target, "--pane", "w1:p2", "--json"])

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)
    assert receipt["disposition"] == "reaped"
    assert receipt["pane"] == "w1:p2"
    assert ("pane", "close", "w1:p2", "--no-focus") in calls


def test_receipt_reap_is_idempotent_after_exact_pane_disappears(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    target = "bh-widget-1"
    current = [_roster_snapshot(target, cwd, state="done")]
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: current[0])
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("pane", "close"):
            current[0] = {"agents": [], "panes": [], "workspaces": []}
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)
    argv = ["plugin", "herdr", "reap", target, "--pane", "w1:p2", "--json"]
    first = runner.invoke(app, argv)
    second = runner.invoke(app, argv)

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["disposition"] == "reaped"
    assert json.loads(second.stdout)["disposition"] == "already_reaped"
    assert [call for call in calls if call[:2] == ("pane", "close")] == [
        ("pane", "close", "w1:p2", "--no-focus")
    ]


def test_receipt_reap_preserves_unrelated_pane_at_expected_locator(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = _roster_snapshot("operator-agent", cwd, tokens=False)
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin.worktree, "managed", lambda _cfg: [])
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    calls = []
    monkeypatch.setattr(
        herdr_plugin,
        "_command",
        lambda *args, **_kwargs: calls.append(args) or _result(),
    )

    result = runner.invoke(
        app,
        ["plugin", "herdr", "reap", "bh-widget-1", "--pane", "w1:p2", "--json"],
    )

    assert result.exit_code == 1
    receipt = json.loads(result.stdout)
    assert receipt["error"]["code"] == "ownership_not_proven"
    assert not any(call[:2] == ("pane", "close") for call in calls)


def test_dispatch_accepts_encoded_target_when_current_roster_proves_ownership(
    tmp_path, monkeypatch
):
    cwd = tmp_path / "widget-1.2"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    bead = "widget-1.2"
    target = herdr_plugin._launch_target(bead)
    snapshot = _roster_snapshot(target, cwd, bead=bead)
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, bead)
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    reads = iter(["agent: idle", "user: ship it\nassistant: working"])
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("agent", "read"):
            return _result(stdout=next(reads))
        return _result(stdout="agent_status: working")

    monkeypatch.setattr(herdr_plugin, "_command", command)

    result = runner.invoke(app, ["plugin", "herdr", "dispatch", target, "ship it", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["disposition"] == "dispatched"
    assert ("agent", "prompt", target, "ship it", "--wait", "--timeout", "60000") in calls


def test_dispatch_refuses_foreign_target_before_sending_prompt(tmp_path, monkeypatch):
    cwd = tmp_path / "manual"
    cwd.mkdir()
    snapshot = _roster_snapshot("manual-agent", cwd, tokens=False)
    snapshot["workspaces"][0]["label"] = "manual"
    snapshot["panes"][0]["label"] = "manual-agent"
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(herdr_plugin.worktree, "managed", lambda _cfg: [])
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    calls = []

    def command(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("agent", "read"):
            return _result(stdout="user: do not send")
        return _result()

    monkeypatch.setattr(herdr_plugin, "_command", command)

    result = runner.invoke(
        app,
        ["plugin", "herdr", "dispatch", "manual-agent", "do not send", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["disposition"] == "refused"
    assert payload["error"]["code"] == "ownership_not_proven"
    assert not any(call[:2] == ("agent", "prompt") for call in calls)


def test_roster_accepts_fully_proven_legacy_target_without_guessing_foreign_panes(
    tmp_path, monkeypatch
):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    legacy = herdr_plugin._roster_payload(
        _roster_snapshot("bh-widget-1", cwd, tokens=False), {"managed_repos": []}
    )["agents"][0]
    foreign_snapshot = _roster_snapshot("manual", cwd, tokens=False)
    foreign_snapshot["panes"][0]["label"] = "manual"
    foreign = herdr_plugin._roster_payload(foreign_snapshot, {"managed_repos": []})["agents"][0]

    assert legacy["bead"] == "widget-1"
    assert legacy["ownership"] == {
        "marker": "legacy-target-v0",
        "association": "legacy",
        "state": "owned",
        "reason": "explicit bh correlation and live resource identities agree",
    }
    assert foreign["hive"] is None
    assert foreign["bead"] is None
    assert foreign["ownership"]["state"] == "foreign"
    assert {item["availability"] for item in foreign["capabilities"].values()} == {"unavailable"}
    foreign_actions = {item["id"]: item for item in foreign["advertised_actions"]}
    assert foreign_actions["agent.dispatch"]["availability"] == "forbidden"
    assert foreign_actions["agent.dispatch"]["reasonCode"] == "agent_not_bh_managed"


def test_roster_retains_metadata_identity_but_disables_stale_missing_worktree(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing-widget"
    target = herdr_plugin._launch_target("widget-1.2")
    _mock_roster_worktree(monkeypatch, tmp_path, missing, "widget-1.2")
    agent = herdr_plugin._roster_payload(
        _roster_snapshot(target, missing, bead="widget-1.2"), {"managed_repos": []}
    )["agents"][0]

    assert agent["hive"] == "github/acme/widgets"
    assert agent["bead"] == "widget-1.2"
    assert agent["worktree"]["state"] == "missing"
    assert agent["ownership"]["state"] == "stale"
    assert "managed worktree is missing" in agent["ownership"]["reason"]
    assert {item["availability"] for item in agent["capabilities"].values()} == {"unavailable"}


def test_ps_json_emits_atomic_versioned_roster(tmp_path, monkeypatch):
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    snapshot = _roster_snapshot("bh-widget-1", cwd)
    monkeypatch.setattr(herdr_plugin, "_has_cli", lambda: True)
    monkeypatch.setattr(herdr_plugin, "server_up", lambda: True)
    monkeypatch.setattr(herdr_plugin, "_session_snapshot", lambda: snapshot)
    monkeypatch.setattr(herdr_plugin.config, "load", lambda: {"managed_repos": []})
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")

    result = runner.invoke(app, ["plugin", "herdr", "ps", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "plugin herdr ps"
    assert payload["agents"][0]["ownership"]["state"] == "owned"


def test_roster_schema_is_valid_and_accepts_the_projection(tmp_path, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    cwd = tmp_path / "widget-1"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: elsewhere\n")
    _mock_roster_worktree(monkeypatch, tmp_path, cwd, "widget-1")
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "schemas"
        / "herdr-agent-roster-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    token = herdr_plugin._SESSION_CONTEXT.set(herdr_plugin._session_selection("operator-team"))
    try:
        payload = herdr_plugin._roster_payload(
            _roster_snapshot("bh-widget-1", cwd), {"managed_repos": []}
        )
    finally:
        herdr_plugin._SESSION_CONTEXT.reset(token)

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        payload
    )
    jsonschema.Draft202012Validator(
        json.loads(_LIFECYCLE_SCHEMA_PATH.read_text()),
        format_checker=jsonschema.FormatChecker(),
    ).validate(payload)
    assert payload["session"] == "operator-team"
    assert payload["agents"][0]["presentation"]["session"] == "operator-team"


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
        "--session",
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


@pytest.mark.parametrize(
    "command", ["status", "ps", "launch", "spawn", "dispatch", "watch", "attach", "reap"]
)
def test_session_aware_command_help_documents_current_selection(command):
    result = runner.invoke(app, ["plugin", "herdr", command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--session" in result.output
    assert "current" in result.output
    assert "BH_HERDR_SESSION" in result.output
    assert "default" in result.output


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
