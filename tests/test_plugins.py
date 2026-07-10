"""plugins.py — the generic plugin seam.

Covers:
- ``registry()`` is import-safe and returns a list.
- a caller iterating the registry and calling hooks inside a guarded try/except loop
  (mirroring the onboard/retire fence) swallows a raising hook and continues to the next
  plugin without leaking state.
"""

from __future__ import annotations

import typer

from beadhive import plugins


def test_registry_is_a_list():
    reg = plugins.registry()
    assert isinstance(reg, list)


def test_registry_is_import_safe_and_callable_twice():
    # Calling it must never raise, even before any plugin module is imported.
    assert plugins.registry() == plugins.registry()


def _mk(name: str, hook):
    return plugins.Plugin(name=name, cli=typer.Typer(), enabled=lambda cfg, entry: True,
                          on_onboard=hook)


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
