"""The normalized CLI-facing alert surface."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import alerts, doctor
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
