"""The normalized CLI-facing alert surface."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import alerts, config, doctor
from beadhive.cli import app


def test_doctor_warnings_are_normalized_alerts(monkeypatch):
    """Existing doctor findings are the first source; no rule is duplicated."""
    monkeypatch.setattr(doctor, "warning_messages", lambda: ["missing required dependency: bd"])
    monkeypatch.setattr(
        config,
        "load",
        lambda: {"alerts": {"worktree_cap_mb": 0, "disk_free_floor_mb": 0}},
    )

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


def test_disk_pressure_alerts_identify_each_constrained_filesystem(monkeypatch):
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
            "worktree_filesystem": {
                "root": "/tmp/bh-worktrees",
                "mount_point": "/tmp",
                "filesystem_type": "tmpfs",
                "device": "tmpfs",
                "device_id": "0:42",
                "free_bytes": 20 * 1024 * 1024 - 1,
            },
            "host_root_filesystem": {
                "root": "/",
                "mount_point": "/",
                "filesystem_type": "ext4",
                "device": "/dev/vda1",
                "device_id": "8:1",
                "free_bytes": 20 * 1024 * 1024 - 1,
            },
        },
    )

    result = alerts.disk_pressure()
    assert [alert.code for alert in result] == [
        "disk.worktree-footprint",
        "disk.worktree-filesystem-free-space",
        "disk.free-space",
    ]
    assert "worktree root '/tmp/bh-worktrees'" in result[1].message
    assert "tmpfs device 'tmpfs' mounted at '/tmp'" in result[1].message
    assert "Free capacity on '/tmp'" in result[1].remediation
    assert "host root filesystem ext4 device '/dev/vda1'" in result[2].message
    assert "Reclaim space on the host root filesystem" in result[2].remediation


def test_disk_pressure_alerts_are_clean_at_configured_boundaries(monkeypatch):
    cfg = {"alerts": {"worktree_cap_mb": 10, "disk_free_floor_mb": 20}}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(
        doctor,
        "_data_worktree_disk_usage",
        lambda _cfg: {
            "hives": [{"prefix": "at-cap", "worktree_bytes": 10 * 1024 * 1024}],
            "disk_free_bytes": 20 * 1024 * 1024,
            "worktree_filesystem": {"free_bytes": 20 * 1024 * 1024},
            "host_root_filesystem": {"free_bytes": 20 * 1024 * 1024},
        },
    )

    assert alerts.disk_pressure() == []


def test_tmpfs_pressure_does_not_report_host_root_as_low(monkeypatch):
    cfg = {
        "alerts": {
            "worktree_cap_mb": 0,
            "worktree_filesystem_free_floor_mb": 10 * 1024,
            "disk_free_floor_mb": 10 * 1024,
        }
    }
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(
        doctor,
        "_data_worktree_disk_usage",
        lambda _cfg: {
            "hives": [],
            "worktree_filesystem": {
                "root": "/tmp/bh-worktrees",
                "mount_point": "/tmp",
                "filesystem_type": "tmpfs",
                "device": "tmpfs",
                "free_bytes": 3 * 1024 * 1024 * 1024,
            },
            "host_root_filesystem": {
                "root": "/",
                "mount_point": "/",
                "filesystem_type": "ext4",
                "device": "/dev/vda1",
                "free_bytes": 27 * 1024 * 1024 * 1024,
            },
        },
    )

    result = alerts.disk_pressure()

    assert [alert.code for alert in result] == ["disk.worktree-filesystem-free-space"]
    assert "3.0 GB free" in result[0].message
    assert "host root" not in result[0].message
    assert "Free capacity on '/tmp'" in result[0].remediation


def test_worktree_filesystem_floor_inherits_legacy_disk_floor():
    assert (
        config.alerts_worktree_filesystem_free_floor_mb({"alerts": {"disk_free_floor_mb": 4096}})
        == 4096
    )
    assert (
        config.alerts_worktree_filesystem_free_floor_mb(
            {"alerts": {"disk_free_floor_mb": 4096, "worktree_filesystem_free_floor_mb": 2048}}
        )
        == 2048
    )
