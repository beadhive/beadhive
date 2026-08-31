"""plugins.py — the generic plugin seam.

Covers:
- ``registry()`` is import-safe and returns a list.
- a caller iterating the registry and calling hooks inside a guarded try/except loop
  (mirroring the onboard/retire fence) swallows a raising hook and continues to the next
  plugin without leaking state.
"""

from __future__ import annotations

import ast
from pathlib import Path

import typer

from beadhive import plugins
from beadhive.kernel.lifecycle import DeliveryStatus


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
    plugin = _mk("sample", lambda ctx: called.append(ctx))
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    assert [(mount.plugin_id, mount.app) for mount in plugins.cli_mounts()] == [
        ("sample", plugin.cli)
    ]
    (participant,) = plugins.onboard_participants()
    context = type("Ctx", (), {"hive": "github/acme/repo"})()
    report = participant.deliver(context)

    assert called == [context]
    assert report.event_id == "hive.onboarding"
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
            if isinstance(node, ast.Attribute) and node.attr in forbidden
            and not (
                node.attr == "readiness"
                and isinstance(node.value, ast.Name)
                and node.value.id == "gitworkspace_plugin"
            )
        }
        if attrs:
            found[filename] = attrs
    assert found == {}


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
