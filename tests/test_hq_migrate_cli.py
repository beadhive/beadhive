"""Public migrate command never publishes without explicit confirmation."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from beadhive import hq_migrate_cli
from beadhive.cli import app


def test_hq_migrate_defaults_to_plan_and_requires_confirm_for_apply(monkeypatch):
    calls = []

    def sql(*, dry_run, confirm, intent):
        calls.append((dry_run, confirm, intent))
        return {"to": "dolt-server", "phase": "dry-run" if dry_run else "CONFIG_READY"}

    monkeypatch.setattr(hq_migrate_cli, "_to_sql", sql)
    preview = CliRunner().invoke(app, ["hq", "migrate", "--to", "dolt-server"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["phase"] == "dry-run"
    assert calls == [(True, False, None)]
    applied = CliRunner().invoke(
        app,
        ["hq", "migrate", "--to", "dolt-server", "--confirm", "--intent", "/tmp/seed.json"],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)["phase"] == "CONFIG_READY"
    assert calls[-1] == (False, True, Path("/tmp/seed.json"))
