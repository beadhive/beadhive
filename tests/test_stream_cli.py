"""Real CLI coverage for the snapshot-first NDJSON stream surface (bh-jksq.4)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from beadhive import bd as bd_mod
from beadhive import state_stream, stream_cli
from beadhive.cli import app

runner = CliRunner()
NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def issue(*, status="open"):
    return state_stream.StreamIssue(
        id="bh-1",
        hive="beadhive",
        issue_type="task",
        status=status,
        priority="P1",
        title="Stream the state",
        updated_at=NOW,
    )


class FiniteProvider:
    name = "finite-test-provider"

    def __init__(self):
        self.requests = []

    def updates(self, request):
        self.requests.append(request)
        yield state_stream.ProviderSnapshot(request.scope, "opaque:first", NOW, (issue(),))
        yield state_stream.ProviderSnapshot(
            request.scope, "opaque:second", NOW, (issue(status="closed"),)
        )


def install_provider(monkeypatch):
    provider = FiniteProvider()
    monkeypatch.setattr(stream_cli.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(stream_cli, "get_polling_provider", lambda _cfg: provider)
    return provider


def test_cli_emits_snapshot_then_delta_as_stdout_only_ndjson(monkeypatch):
    provider = install_provider(monkeypatch)

    result = runner.invoke(app, ["stream", "--scope", "hub", "--format", "ndjson"])

    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["frame"] for row in rows] == ["snapshot", "delta"]
    assert rows[0]["revision"] == "opaque:first"
    assert rows[0]["reason"] == "initial"
    assert rows[1]["since_revision"] == "opaque:first"
    assert rows[1]["revision"] == "opaque:second"
    assert rows[1]["changed"][0]["status"] == "closed"
    assert "opaque:first" not in result.stderr
    assert '"frame"' not in result.stderr
    assert provider.requests == [state_stream.StreamRequest("hub")]


def test_cli_passes_opaque_since_without_weakening_snapshot_first(monkeypatch):
    provider = install_provider(monkeypatch)

    result = runner.invoke(
        app,
        ["stream", "--scope", "factory", "--since", "do:not:parse:this"],
    )

    assert result.exit_code == 0, result.output
    first = json.loads(result.stdout.splitlines()[0])
    assert first["frame"] == "snapshot"
    assert provider.requests == [
        state_stream.StreamRequest("factory", since_revision="do:not:parse:this")
    ]


def test_cli_consumer_boundary_never_spawns_bd_directly(monkeypatch):
    install_provider(monkeypatch)

    def forbidden_bd(*_args, **_kwargs):
        raise AssertionError("the CLI consumer boundary reached past its provider into bd")

    monkeypatch.setattr(bd_mod, "_run", forbidden_bd)

    result = runner.invoke(app, ["stream", "--scope", "hub"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.splitlines()[0])["frame"] == "snapshot"


def test_hive_scope_resolves_hive_after_the_stream_command(monkeypatch):
    provider = install_provider(monkeypatch)
    entry = {"provider": "github", "org": "beadhive", "repo": "beadhive", "prefix": "bh"}
    monkeypatch.setattr(stream_cli.registry, "resolve_hive", lambda _cfg, value: entry)

    result = runner.invoke(app, ["stream", "--scope", "hive", "--hive", "bh"])

    assert result.exit_code == 0, result.output
    assert provider.requests == [state_stream.StreamRequest("hive", hive="beadhive")]


def test_hive_scope_outside_a_managed_hive_is_a_stderr_only_diagnostic(monkeypatch):
    install_provider(monkeypatch)
    monkeypatch.setattr(stream_cli.registry, "current_hive", lambda _cfg: None)

    result = runner.invoke(app, ["stream", "--scope", "hive"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "hive scope needs --hive" in result.stderr


class FlushRecorder(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


def test_transport_flushes_every_lf_terminated_frame():
    provider = FiniteProvider()
    output = FlushRecorder()

    stream_cli.emit_ndjson(
        state_stream.stream_frames(provider, state_stream.StreamRequest("hub")), output
    )

    assert output.flushes == 2
    assert output.getvalue().endswith("\n")
    assert all(line.startswith("{") for line in output.getvalue().splitlines())
