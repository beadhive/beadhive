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
