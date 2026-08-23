"""Static contract tests for the installable SAFE-only prune cadence."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scheduled_prune_wrapper_uses_the_safe_only_fleet_command():
    wrapper = (ROOT / "scripts" / "bh-worktree-prune").read_text()

    assert 'exec "${BH_BIN:-bh}" worktree prune' in wrapper


def test_systemd_timer_is_persistent_and_has_a_configurable_override():
    timer = (ROOT / "scripts" / "bh-worktree-prune.timer").read_text()
    installer = (ROOT / "scripts" / "install-worktree-prune-timer.sh").read_text()

    assert "Persistent=true" in timer
    assert "--interval" in installer
    assert "OnUnitActiveSec=%s" in installer
    assert "--verify" in installer
    assert "--uninstall" in installer
