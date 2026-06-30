"""work-setting resolution — demo_cmd follows the per-rig > global > default tiers (work_value)."""

from __future__ import annotations

from ws import config


def test_demo_cmd_default_empty_when_unset():
    assert config.demo_cmd({}, None) == ""
    assert config.demo_cmd({"work": {}}, {}) == ""


def test_demo_cmd_global_then_per_rig_override():
    cfg = {"work": {"demo_cmd": "just demo"}}
    # global wins when the rig has no override
    assert config.demo_cmd(cfg, {}) == "just demo"
    # per-rig entry overrides the global
    assert config.demo_cmd(cfg, {"work": {"demo_cmd": "make run"}}) == "make run"


def test_validate_cmd_default_and_per_phase():
    assert config.validate_cmd({}, None) == "just check"  # hard default
    cfg = {"work": {"validate_cmd": "just check", "validate": {"molecule": "just check-all"}}}
    assert config.validate_cmd(cfg, {}, "molecule") == "just check-all"  # per-phase override
    assert config.validate_cmd(cfg, {}, "submit") == "just check"  # unset phase → validate_cmd


def test_validate_cmd_main_gate_prefers_phase_main_variant():
    cfg = {"work": {"validate_cmd": "just check", "validate": {"merge-main": "just check-all"}}}
    # ad-hoc bead → main: main_gate prefers the `-main` variant
    assert config.validate_cmd(cfg, {}, "merge", main_gate=True) == "just check-all"
    # molecule member → mol/<epic>: plain phase, falls through to validate_cmd
    assert config.validate_cmd(cfg, {}, "merge", main_gate=False) == "just check"
    # main_gate falls back to the plain phase when no `-main` key exists
    cfg2 = {"work": {"validate_cmd": "just check", "validate": {"merge": "just test"}}}
    assert config.validate_cmd(cfg2, {}, "merge", main_gate=True) == "just test"
