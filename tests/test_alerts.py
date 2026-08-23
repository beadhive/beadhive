"""The normalized CLI-facing alert surface."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import alerts, config, doctor
from beadhive.cli import app


def test_doctor_warnings_are_normalized_alerts(monkeypatch):
    """Existing doctor findings are the first source; no rule is duplicated."""
    monkeypatch.setattr(doctor, "warning_messages", lambda: ["missing required dependency: bd"])

    assert alerts.active() == [
        {
            "severity": "warning",
            "code": "doctor.warning",
            "message": "missing required dependency: bd",
            "remediation": (
                "Run `bh doctor` for the full diagnostic context, then address the condition "
                "named in this alert."
            ),
        }
    ]


def test_alerts_show_json_and_clean_human_render(monkeypatch):
    """Machine consumers receive a list; a clean human result is explicit."""
    runner = CliRunner()
    monkeypatch.setattr(alerts, "active", lambda: [])
    clean = runner.invoke(app, ["alerts", "show"])
    assert clean.exit_code == 0
    assert clean.output == "✓ no active alerts\n"

    rows = [
        {
            "severity": "warning",
            "code": "test.warning",
            "message": "be careful",
            "remediation": "fix it",
        }
    ]
    monkeypatch.setattr(alerts, "active", lambda: rows)
    machine = runner.invoke(app, ["alerts", "show", "--json"])
    assert machine.exit_code == 0
    assert json.loads(machine.output) == rows


def test_disk_pressure_alerts_fire_only_over_configured_boundaries(monkeypatch):
    cfg = {"alerts": {"worktree_cap_mb": 10, "disk_free_floor_mb": 20}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(
        doctor,
        "_data_worktree_disk_usage",
        lambda _cfg: {
            "hives": [
                {"prefix": "under", "worktree_bytes": 10 * 1024 * 1024},
                {"prefix": "over", "worktree_bytes": 10 * 1024 * 1024 + 1},
            ],
            "disk_free_bytes": 20 * 1024 * 1024 - 1,
        },
    )

    assert [alert.code for alert in alerts.disk_pressure()] == [
        "disk.worktree-footprint",
        "disk.free-space",
    ]


def test_disk_pressure_alerts_are_clean_at_configured_boundaries(monkeypatch):
    cfg = {"alerts": {"worktree_cap_mb": 10, "disk_free_floor_mb": 20}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(
        doctor,
        "_data_worktree_disk_usage",
        lambda _cfg: {
            "hives": [{"prefix": "at-cap", "worktree_bytes": 10 * 1024 * 1024}],
            "disk_free_bytes": 20 * 1024 * 1024,
        },
    )

    assert alerts.disk_pressure() == []
