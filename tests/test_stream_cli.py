"""Real CLI coverage for the snapshot-first NDJSON stream surface (bh-jksq.4)."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import bd as bd_mod
from beadhive import run as run_mod
from beadhive import state_stream, state_stream_polling, stream_cli
from beadhive.cli import app
from beadhive.state_stream_process import StreamProcessScope

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
    monkeypatch.setattr(
        stream_cli.config, "load", lambda: {"schema_version": 1, "managed_repos": []}
    )
    monkeypatch.setattr(stream_cli, "get_polling_provider", lambda _cfg, *, process_scope: provider)
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


def _fake_wedged_bd(tmp_path: Path) -> tuple[Path, Path]:
    """A bd-shaped executable whose direct child and grandchild both wedge."""

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    pid_path = tmp_path / "backend-grandchild.pid"
    binary = binary_dir / "bd"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os,time\n"
        "child=os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "open(os.environ['BH_TEST_DESCENDANT_PID'], 'w').write(str(child))\n"
        "time.sleep(300)\n"
    )
    binary.chmod(0o755)
    return binary_dir, pid_path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


def _wait_pid(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text().strip()
        except FileNotFoundError:
            value = ""
        if value:
            return int(value)
        time.sleep(0.02)
    raise AssertionError(f"backend did not publish a descendant pid to {path}")


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="real process-group assertion uses /proc")
def test_cli_timeout_reaps_the_backend_grandchild(tmp_path, monkeypatch):
    binary_dir, pid_path = _fake_wedged_bd(tmp_path)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BH_TEST_DESCENDANT_PID", str(pid_path))
    monkeypatch.setattr(stream_cli.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(state_stream_polling.config, "hub_dir", lambda: tmp_path / "hub")
    monkeypatch.setattr(
        stream_cli,
        "StreamProcessScope",
        lambda: StreamProcessScope(timeout=0.2, term_grace=0.1),
    )

    result = runner.invoke(app, ["stream", "--scope", "hub"])

    grandchild = _wait_pid(pid_path)
    assert result.exit_code == 1
    assert isinstance(result.exception, run_mod.ChildTimeout)
    assert _wait_gone(grandchild), "bh stream timeout left a backend descendant alive"


def test_cli_broken_pipe_crosses_the_process_scope_finalizer(monkeypatch):
    provider = install_provider(monkeypatch)
    observed = []

    class RecordingScope:
        def __enter__(self):
            observed.append("enter")
            return self

        def __exit__(self, exc_type, _exc, _tb):
            observed.append(exc_type)

    monkeypatch.setattr(stream_cli, "StreamProcessScope", RecordingScope)

    def broken_output(_frames):
        raise BrokenPipeError("consumer closed stdout")

    monkeypatch.setattr(stream_cli, "emit_ndjson", broken_output)

    result = runner.invoke(app, ["stream", "--scope", "hub"])

    assert result.exit_code == 0
    assert result.exception is None
    assert result.stdout == ""
    assert result.stderr == ""
    assert observed == ["enter", BrokenPipeError]
    assert provider.requests == []  # output failed before pulling the lazy frame iterator


@pytest.mark.skipif(sys.platform != "linux", reason="real process-group assertion uses /proc")
@pytest.mark.parametrize("cancel_signal", [signal.SIGTERM, signal.SIGINT])
def test_real_cli_signal_exit_is_native_and_reaps_backend_tree(
    tmp_path, monkeypatch, cancel_signal
):
    binary_dir, pid_path = _fake_wedged_bd(tmp_path)
    env = dict(os.environ)
    env["PATH"] = f"{binary_dir}{os.pathsep}{env['PATH']}"
    env["BH_TEST_DESCENDANT_PID"] = str(pid_path)
    command = Path(sys.executable).parent / "bh"
    proc = subprocess.Popen(
        [str(command), "stream", "--scope", "hub"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        grandchild = _wait_pid(pid_path)
        proc.send_signal(cancel_signal)
        proc.wait(timeout=10)
        expected = 130 if cancel_signal is signal.SIGINT else -signal.SIGTERM
        assert proc.returncode == expected
        assert _wait_gone(grandchild), (
            f"bh stream {cancel_signal.name} left a backend descendant alive"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
