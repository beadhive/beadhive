"""End-to-end and degradation coverage for ``bh host dispatch runs`` (bh-6eu2c.4).

The reader's unit suite proves individual replay transitions. These tests cross the public CLI
boundary from an actual JSONL sink through the payload so a serialization or command-wiring bug
cannot leave the operator surface green while the underlying reader alone works.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import host_cli
from beadhive.cli import app

runner = CliRunner()


def _wire_sink(monkeypatch, tmp_path, lines):
    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    sink = tmp_path / "dispatch" / "github-acme-widgets.jsonl"
    monkeypatch.setattr(host_cli, "_dispatch_entry", lambda hive, cfg: (entry, "acme/widgets"))
    monkeypatch.setattr(host_cli.dispatch_log, "sink_path", lambda cfg, got: sink)
    sink.parent.mkdir()
    sink.write_text(
        "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n"
    )
    return sink


def test_runs_projects_terminal_cancelled_and_waiting_states_end_to_end(monkeypatch, tmp_path):
    sink = _wire_sink(
        monkeypatch,
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-finished",
                "role": "developer",
                "session_id": "session-finished",
            },
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:01Z",
                "in_flight": ["bh-finished"],
                "denied": [],
                "decision": {"beads": []},
            },
            {
                "event": "seat_harvested",
                "timestamp": "2026-01-01T00:00:02Z",
                "bead": "bh-finished",
                "outcome": "done",
                "exit_code": 0,
                "session_id": "session-finished",
            },
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:03Z",
                "bead": "bh-incomplete",
                "role": "developer",
                "session_id": "session-incomplete",
            },
            {
                "event": "seat_harvested",
                "timestamp": "2026-01-01T00:00:04Z",
                "bead": "bh-incomplete",
                "outcome": "incomplete",
                "exit_code": 1,
                "session_id": "session-incomplete",
            },
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:05Z",
                "bead": "bh-cancelled",
                "role": "reviewer",
                "session_id": "session-cancelled",
            },
            {
                "event": "seat_cancelled",
                "timestamp": "2026-01-01T00:00:06Z",
                "bead": "bh-cancelled",
                "rung": "hard",
                "exit_code": 137,
                "priced": True,
                "session_id": "session-cancelled",
                "group_gone": False,
                "signals": ["SIGKILL"],
            },
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:07Z",
                "in_flight": [],
                "denied": [{"reason": "concurrency_cap", "detail": "full"}],
                "decision": {"beads": ["bh-waiting"]},
            },
            # An actively-appending writer can leave a final torn line. The valid projection
            # before it must survive unchanged rather than failing or dropping every summary.
            '{"event":"seat_spawned","bead":"bh-torn"',
        ],
    )

    result = runner.invoke(app, ["host", "dispatch", "runs", "--hive", "acme/widgets", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["scope"] == "host-local"
    assert payload["sink_path"] == str(sink)
    by_session = {run["session_id"]: run for run in payload["runs"]}
    assert by_session["session-finished"]["state"] == "finished"
    assert by_session["session-incomplete"]["state"] == "failed"
    assert by_session["session-cancelled"]["state"] == "failed"
    assert by_session[""]["bead"] == "bh-waiting"
    assert by_session[""]["state"] == "waiting"
    assert {run["freshness"]["state"] for run in payload["runs"]} == {"unknown"}
    assert "bh-torn" not in {run["bead"] for run in payload["runs"]}


def test_runs_json_missing_sink_degrades_to_empty_host_local_view(monkeypatch, tmp_path):
    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    missing = tmp_path / "dispatch" / "missing.jsonl"
    monkeypatch.setattr(host_cli, "_dispatch_entry", lambda hive, cfg: (entry, "acme/widgets"))
    monkeypatch.setattr(host_cli.dispatch_log, "sink_path", lambda cfg, got: missing)

    result = runner.invoke(app, ["host", "dispatch", "runs", "--hive", "acme/widgets", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "scope": "host-local",
        "hive": "acme/widgets",
        "sink_path": str(missing),
        "runs": [],
    }


def test_runs_human_output_names_sink_and_renders_projected_states(monkeypatch, tmp_path):
    sink = _wire_sink(
        monkeypatch,
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-finished",
                "role": "developer",
                "session_id": "session-finished",
            },
            {
                "event": "seat_harvested",
                "timestamp": "2026-01-01T00:00:01Z",
                "bead": "bh-finished",
                "outcome": "done",
                "exit_code": 0,
                "session_id": "session-finished",
            },
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:02Z",
                "in_flight": [],
                "denied": [{"reason": "concurrency_cap", "detail": "full"}],
                "decision": {"beads": ["bh-waiting"]},
            },
        ],
    )

    result = runner.invoke(app, ["host", "dispatch", "runs", "--hive", "acme/widgets"])

    assert result.exit_code == 0, result.output
    assert f"HOST-LOCAL AgentRunSummary view for acme/widgets — sink={sink}" in result.output
    assert "bh-finished" in result.output
    assert "session-finished" in result.output
    assert "finished" in result.output
    assert "bh-waiting" in result.output
    assert "waiting" in result.output
    assert result.output.count("freshness=unknown") == 2
