"""plugins.py — the generic plugin seam.

Covers:
- ``registry()`` is import-safe and returns a list.
- a caller iterating the registry and calling hooks inside a guarded try/except loop
  (mirroring the onboard/retire fence) swallows a raising hook and continues to the next
  plugin without leaking state.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import typer
from typer.testing import CliRunner

from beadhive import cli as cli_module
from beadhive import config, orca, plugins
from beadhive.kernel.lifecycle import EVENTS_BY_ID, DeliveryStatus


def test_registry_is_a_list():
    reg = plugins.registry()
    assert isinstance(reg, list)


def test_registry_is_import_safe_and_callable_twice():
    # Calling it must never raise, even before any plugin module is imported.
    assert plugins.registry() == plugins.registry()


def _mk(name: str, hook):
    return plugins.Plugin(
        name=name, cli=typer.Typer(), enabled=lambda cfg, entry: True, on_onboard=hook
    )


def test_fenced_loop_swallows_a_raising_hook_and_continues():
    """A raising on_onboard hook must not stop the loop reaching the next plugin."""
    called: list[str] = []

    def boom(ctx):
        called.append("boom")
        raise RuntimeError("plugin exploded")

    def ok(ctx):
        called.append("ok")

    reg = [_mk("boom", boom), _mk("ok", ok)]

    # Mirror the caller fence: guard each hook, warn-and-continue on failure.
    warnings: list[str] = []
    for p in reg:
        if p.on_onboard is None:
            continue
        try:
            p.on_onboard(ctx=None)
        except Exception as exc:  # noqa: BLE001 - the fence swallows everything
            warnings.append(f"{p.name}: {exc}")

    assert called == ["boom", "ok"]  # both ran; the raise did not abort the loop
    assert warnings == ["boom: plugin exploded"]


def test_plugin_is_frozen():
    p = _mk("x", lambda ctx: None)
    try:
        p.name = "y"
    except Exception:
        return
    raise AssertionError("Plugin should be frozen (immutable)")


def test_compatibility_facade_projects_cli_and_typed_lifecycle(monkeypatch):
    called: list[object] = []
    plugin = _mk("orca", lambda ctx: called.append(ctx))
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    assert [(mount.plugin_id, mount.app) for mount in plugins.cli_mounts()] == [
        ("orca", plugin.cli)
    ]
    (prototype,) = plugins.onboard_participants()
    participant = prototype.resolve({}, {})
    context = type("Ctx", (), {"hive": "github/acme/repo"})()
    report = participant.deliver(context)

    assert called == [context]
    assert report.event_id == "hive.onboarding"
    assert report.deliveries[0].subscription_id == "orca.register-hive"
    assert report.deliveries[0].status is DeliveryStatus.SUCCEEDED


def test_runtime_callers_do_not_inspect_nullable_plugin_callbacks():
    forbidden = {
        "on_onboard",
        "on_retire",
        "readiness",
        "wt_create",
        "wt_remove",
        "wt_creating",
        "wt_created",
    }
    root = Path(__file__).parents[1]
    callers = ("cli.py", "hive_ready.py", "onboard.py", "retire.py", "worktree.py")
    found: dict[str, set[str]] = {}
    for filename in callers:
        tree = ast.parse((root / "src" / "beadhive" / filename).read_text(encoding="utf-8"))
        attrs = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in forbidden
            and not (
                node.attr == "readiness"
                and isinstance(node.value, ast.Name)
                and node.value.id == "gitworkspace_plugin"
            )
        }
        if attrs:
            found[filename] = attrs
    assert found == {}


def test_compatibility_facade_delegates_builtin_metadata_and_enablement(monkeypatch):
    names = ("herdr", "hitch", "observaloop", "orca", "repowise")
    declarations = [
        plugins.Plugin(name, typer.Typer(), enabled=lambda cfg, entry: True) for name in names
    ]
    monkeypatch.setattr(plugins, "registry", lambda: declarations)

    result = plugins.discover_builtin_registry(
        {},
        {},
        host_executables={name: "1.0.0" for name in names},
    )

    assert [plugin.manifest.plugin_id for plugin in result.plugins] == list(names)
    assert {selection.plugin_id for selection in result.capabilities} == set(names)
    assert result.errors == ()


def test_kernel_disablement_overrides_legacy_enablement_and_never_runs_callback(monkeypatch):
    calls: list[object] = []

    def callback(*args, **kwargs):
        calls.append((args, kwargs))

    plugin = plugins.Plugin(
        "orca",
        typer.Typer(),
        enabled=lambda cfg, entry: True,
        on_onboard=callback,
        on_retire=callback,
        readiness=callback,
        wt_create=callback,
        wt_remove=callback,
        wt_creating=callback,
        wt_created=callback,
    )
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])
    cfg = {
        "plugin_kernel": {
            "enabled": {
                plugin_id: False
                for plugin_id in ("herdr", "hitch", "observaloop", "orca", "repowise")
            }
        }
    }

    result = plugins.discover_builtin_registry(cfg, {})
    (prototype,) = plugins.onboard_participants()
    participant = prototype.resolve(cfg, {})
    context = type(
        "Ctx",
        (),
        {
            "cfg": cfg,
            "existing": {},
            "plugins": [],
            "hive": "github/acme/repo",
        },
    )()

    assert result.plugins == ()
    assert result.capabilities == ()
    assert participant.enabled() is False
    assert participant.deliver(context).deliveries == ()
    (readiness,) = plugins.readiness_ports(cfg, {})
    assert readiness.enabled() is False
    assert readiness.probe(cfg, {}) is None
    assert plugins.retire_observers(cfg, {}) == ()
    assert plugins.worktree_create_ports(cfg, {}) == ()
    assert plugins.worktree_remove_ports(cfg, {}) == ()
    assert plugins.worktree_observers("wt_creating", cfg, {}) == ()
    assert plugins.worktree_observers("wt_created", cfg, {}) == ()
    assert calls == []


def test_invalid_kernel_config_fails_before_legacy_registry_access(monkeypatch):
    monkeypatch.setattr(
        plugins,
        "registry",
        lambda: (_ for _ in ()).throw(AssertionError("legacy registry must not be read")),
    )

    result = plugins.discover_builtin_registry(
        {"plugin_kernel": {"enabled": {"orca": "yes"}}},
        {},
    )

    assert result.plugins == ()
    assert result.capabilities == ()
    assert [diagnostic.code.value for diagnostic in result.errors] == ["invalid-config"]


def test_canonical_disablement_removes_optional_cli_commands_but_not_required_dep(monkeypatch):
    cfg = {
        "orca": {"enabled": True},
        "plugin_kernel": {
            "enabled": {
                plugin_id: False
                for plugin_id in ("herdr", "hitch", "observaloop", "orca", "repowise")
            }
        },
    }
    calls: list[object] = []
    assert plugins.cli_mounts(cfg, {}) == ()

    with monkeypatch.context() as scoped:
        scoped.setattr(config, "load", lambda: cfg)
        scoped.setattr(orca, "sync_repos", lambda *args, **kwargs: calls.append((args, kwargs)))
        disabled_cli = importlib.reload(cli_module)
        result = CliRunner().invoke(
            disabled_cli.app,
            ["plugin", "orca", "sync", "--dry-run"],
        )
        help_result = CliRunner().invoke(disabled_cli.app, ["plugin", "--help"])

    importlib.reload(cli_module)
    assert result.exit_code != 0
    assert calls == []
    assert "git-workspace" in help_result.output


def test_readiness_snapshot_evaluates_predicate_and_manifest_source_once(monkeypatch):
    predicate_calls: list[bool] = []
    source_calls: list[bool] = []
    probe_calls: list[bool] = []
    original_source = plugins.builtin_manifest_source

    def enabled(_cfg, _entry):
        predicate_calls.append(True)
        return len(predicate_calls) == 1

    def source():
        source_calls.append(True)
        if len(source_calls) > 1:
            raise AssertionError("manifest source was re-evaluated")
        return original_source()

    def readiness(_cfg, _entry):
        probe_calls.append(True)
        return "ok", "stable snapshot"

    plugin = plugins.Plugin("orca", typer.Typer(), enabled=enabled, readiness=readiness)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])
    monkeypatch.setattr(plugins, "builtin_manifest_source", source)

    (port,) = plugins.readiness_ports({}, {})
    assert port.enabled() is True
    assert port.probe({}, {}) == ("ok", "stable snapshot")
    assert predicate_calls == [True]
    assert source_calls == [True]
    assert probe_calls == [True]


def test_runtime_lifecycle_bindings_match_every_manifest_declaration():
    hook_events = {
        "on_onboard": "hive.onboarding",
        "on_retire": "hive.retiring",
        "readiness": "host.readiness",
        "wt_creating": "worktree.creating",
        "wt_created": "worktree.created",
    }
    composition = plugins._compose(None, None, honor_legacy_enablement=False)
    expected: dict[tuple[str, str], object] = {}
    for discovered in composition.result.plugins:
        for declaration in discovered.manifest.lifecycle_subscriptions:
            expected[(discovered.manifest.plugin_id, declaration.event)] = declaration

    actual = {
        (plugin.name, event_id)
        for plugin in composition.declarations
        for hook, event_id in hook_events.items()
        if getattr(plugin, hook) is not None
    }
    assert actual == set(expected)

    async def subscriber(_context) -> None:
        pass

    for (plugin_id, event_id), declaration in expected.items():
        binding = plugins._manifest_binding(
            composition,
            plugin_id,
            event_id,
            subscriber,
        )
        assert binding.subscription_id == declaration.subscription_id
        assert binding.event is EVENTS_BY_ID[event_id]
        assert binding.policy == declaration.policy


def test_readiness_failure_is_dispatched_and_warns_without_raising(monkeypatch):
    def crash(_cfg, _entry):
        raise RuntimeError("probe exploded")

    plugin = plugins.Plugin(
        "orca",
        typer.Typer(),
        enabled=lambda cfg, entry: True,
        readiness=crash,
    )
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    (port,) = plugins.readiness_ports({}, {})
    assert port.probe({}, {}) == (
        "off",
        "readiness probe failed (RuntimeError: probe exploded)",
    )


# ---- worktree create/remove hooks --------------------------------------------


def test_worktree_hooks_default_to_none():
    # A plugin that never mentions the worktree-delegation hooks stays fully generic (no
    # integration is hardcoded) — both fields must default to None.
    p = _mk("x", lambda ctx: None)
    assert p.wt_create is None
    assert p.wt_remove is None
    assert p.wt_creating is None
    assert p.wt_created is None
    assert p.onboard_requires_opt_in is False


def test_worktree_hooks_are_settable():
    p = plugins.Plugin(
        name="x",
        cli=typer.Typer(),
        enabled=lambda cfg, entry: True,
        wt_create=lambda cfg, entry, **kw: None,
        wt_remove=lambda cfg, entry, **kw: False,
        wt_creating=lambda cfg, entry, **kw: None,
        wt_created=lambda cfg, entry, **kw: None,
    )
    assert p.wt_create is not None
    assert p.wt_remove is not None
    assert p.wt_creating is not None
    assert p.wt_created is not None
