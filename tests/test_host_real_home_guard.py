"""The real-home guard catches a host/HQ test that escapes its sandbox (bh-7zu86).

`test_host_adopt.py`'s half-state tests once called ``monkeypatch.undo()``, which also dropped
the BH_HOME/HOME sandbox, so the next real adopt read this host's LIVE ``host.yaml`` and HQ
lease. These negative tests reproduce that escape on purpose and prove the guard stops it at
path resolution — before any read, write or HQ connection. Every escaping call below only
computes a path; the guard raises before it is returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive import config, host, hq_operator_settings
from stateful_fixtures import _OPERATOR_ROOTS, RealHomeEscape


def test_the_sandboxed_home_is_allowed(real_home_guard):
    assert real_home_guard.armed
    assert host.path().parent == config.home()
    assert real_home_guard.escapes == []


def test_monkeypatch_undo_then_resolving_the_home_is_refused(real_home_guard, monkeypatch):
    """The exact bh-7zu86 escape: undo drops BH_HOME, and the real home is next."""
    monkeypatch.undo()
    with pytest.raises(RealHomeEscape, match="operator state"):
        config.home()
    with pytest.raises(RealHomeEscape):
        host.path()  # host.yaml is never even named, let alone opened
    assert real_home_guard.escapes
    real_home_guard.escapes.clear()  # expected here; any other test fails at teardown


def test_an_escape_swallowed_by_production_code_still_fails_the_test(real_home_guard, monkeypatch):
    monkeypatch.delenv("BH_HOME")
    try:
        config.home()
    except BaseException:  # noqa: BLE001 - simulating an over-broad fallback
        pass
    assert len(real_home_guard.escapes) == 1, "recorded for the teardown check"
    real_home_guard.escapes.clear()


def test_operator_hq_settings_under_the_real_home_are_refused_before_reading(real_home_guard):
    # `~` would expand against the SANDBOX HOME here; the guard captured the real one up front.
    real_settings = _OPERATOR_ROOTS[0] / "hq-operator.yaml"
    assert real_settings.parent.name == ".beadhive"
    with pytest.raises(RealHomeEscape):
        hq_operator_settings.load_settings(real_settings)
    real_home_guard.escapes.clear()


def test_the_real_home_dir_itself_is_refused(real_home_guard, monkeypatch):
    real_home = next((r for r in _OPERATOR_ROOTS if r.name not in (".beadhive", ".ws")), None)
    if real_home is None:
        pytest.skip("no operator HOME claimed (hermetic fence: HOME is already private)")
    monkeypatch.setenv("HOME", str(real_home))
    with pytest.raises(RealHomeEscape):
        Path.home()
    real_home_guard.escapes.clear()
