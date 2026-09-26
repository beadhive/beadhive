"""Supervised-service owner, endpoint record, and resolver over a loopback stand-in ``bd``.

The stand-in answers only the negotiation routes (health, context, one ready row) on
``127.0.0.1``; it models the process contract of ``bd serve`` (argv, token file, SIGTERM), not
Beads behavior. No network beyond loopback is used.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from beadhive_beads_client import IncompatibleService, RemoteEndpoint
from beadhive_beads_client import service as svc

FAKE_BD = r"""
import json, os, signal, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
repo = args[args.index("-C") + 1]
host, port = args[args.index("--addr") + 1].rsplit(":", 1)
token_file = args[args.index("--auth-token-file") + 1]
with open(os.path.join(repo, "fake-context.json")) as stream:
    context = json.load(stream)
if context.pop("_exit_immediately", False):
    sys.exit(3)
PROBLEM = {"status": 401, "title": "Unauthorized", "code": "unauthorized", "request_id": "r1"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self.reply(200, {"status": "ok"})
        with open(token_file) as stream:
            tokens = {line.strip() for line in stream if line.strip()}
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or header[7:] not in tokens:
            return self.reply(401, PROBLEM)
        if path == "/v0/beads/context":
            return self.reply(200, context)
        if path == "/v0/beads/ready":
            return self.reply(200, {"items": [], "has_more": False})
        return self.reply(404, dict(PROBLEM, status=404, code="not_found"))


server = ThreadingHTTPServer((host, int(port)), Handler)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
print(f"bd serve: listening on http://{host}:{port}", flush=True)
server.serve_forever()
"""

CAPABILITIES = ["project.enforce", "issues.get", "issues.list", "ready.list"]


def _context(project_id: str = "proj-1", database: str = "hive_db") -> dict[str, object]:
    return {
        "api_version": "v0",
        "bd_version": "1.3.0",
        "schema_version": 1,
        "backend": "dolt",
        "dolt_mode": "server",
        "database": database,
        "project_id": project_id,
        "capabilities": CAPABILITIES,
    }


@pytest.fixture
def spec(tmp_path: Path) -> svc.ServiceSpec:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fake-context.json").write_text(json.dumps(_context()))
    bd = tmp_path / "bd"
    bd.write_text(f"#!{sys.executable}\n{FAKE_BD}")
    bd.chmod(0o755)
    return svc.ServiceSpec(
        workspace="github/acme/widgets",
        repo_root=repo,
        project_id="proj-1",
        database="hive_db",
        bd_executable=bd,
        paths=svc.ServicePaths(tmp_path / "run" / "github-acme-widgets"),
        start_command="bh host beads start --hive github/acme/widgets",
        startup_seconds=10.0,
        stop_seconds=5.0,
        probe_seconds=2.0,
    )


@pytest.fixture(autouse=True)
def _reap(spec: svc.ServiceSpec):
    yield
    record, _ = svc.load_record(spec.paths)
    if record is not None and svc.process_alive(record.pid):
        os.kill(record.pid, signal.SIGKILL)


def _wait_dead(pid: int) -> None:
    deadline = time.monotonic() + 5
    while svc.process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_start_publishes_private_token_free_record_and_resolves(spec: svc.ServiceSpec) -> None:
    outcome = svc.start(spec)

    assert outcome.started is True
    record = outcome.record
    assert (record.address, record.workspace, record.bd_version) == (
        "127.0.0.1",
        "github/acme/widgets",
        "1.3.0",
    )
    token = svc.read_token(spec.paths.token)
    raw = spec.paths.record.read_text()
    assert token not in raw
    assert json.loads(raw)["token_file"] == str(spec.paths.token)
    assert stat.S_IMODE(spec.paths.token.stat().st_mode) == 0o600
    assert stat.S_IMODE(spec.paths.record.stat().st_mode) == 0o600
    assert stat.S_IMODE(spec.paths.directory.stat().st_mode) == 0o700
    assert token not in spec.paths.log.read_text()

    endpoint = svc.resolve(spec)
    assert isinstance(endpoint, RemoteEndpoint)
    assert endpoint.url == f"http://127.0.0.1:{record.port}"
    assert endpoint.token == token
    assert token not in repr(endpoint)
    assert svc.status(spec).state == "running"


def test_second_start_is_idempotent(spec: svc.ServiceSpec) -> None:
    first = svc.start(spec)
    second = svc.start(spec)

    assert second.started is False
    assert second.process is None
    assert second.record.pid == first.record.pid
    assert second.record.port == first.record.port


def test_stop_sigterms_removes_record_and_resolver_fails_closed(spec: svc.ServiceSpec) -> None:
    record = svc.start(spec).record

    outcome = svc.stop(spec)

    assert (outcome.state_before, outcome.pid, outcome.signalled) == ("running", record.pid, True)
    _wait_dead(record.pid)
    assert not svc.process_alive(record.pid)
    assert not spec.paths.record.exists()
    assert svc.stop(spec).state_before == "absent"
    with pytest.raises(svc.ServiceUnavailable) as failure:
        svc.resolve(spec)
    assert failure.value.state == "absent"
    assert "start it with `bh host beads start --hive github/acme/widgets`" in str(failure.value)


def test_crash_leaves_stale_record_that_fails_closed_then_restarts(
    spec: svc.ServiceSpec,
) -> None:
    crashed = svc.start(spec).record
    os.kill(crashed.pid, signal.SIGKILL)
    _wait_dead(crashed.pid)

    assert svc.status(spec).state == "stale"
    with pytest.raises(svc.ServiceUnavailable, match="stale.*start it with"):
        svc.resolve(spec)

    restarted = svc.start(spec)
    assert restarted.started is True
    assert restarted.record.pid != crashed.pid
    assert svc.resolve(spec).url == restarted.record.url


def test_record_for_another_workspace_is_a_mismatch(spec: svc.ServiceSpec) -> None:
    svc.start(spec)
    other = svc.ServiceSpec(**{**spec.__dict__, "database": "other_db"})

    with pytest.raises(svc.ServiceUnavailable, match="mismatch.*database") as failure:
        svc.resolve(other)
    assert failure.value.state == "mismatch"
    with pytest.raises(svc.ServiceConflict, match="alive but mismatch"):
        svc.start(other)


def test_live_context_mismatch_fails_closed(spec: svc.ServiceSpec) -> None:
    (spec.repo_root / "fake-context.json").write_text(json.dumps(_context(project_id="imposter")))
    with pytest.raises(IncompatibleService, match="identity mismatch"):
        svc.start(spec)
    assert not spec.paths.record.exists()


def test_record_whose_live_service_answers_for_another_project_fails_closed(
    spec: svc.ServiceSpec,
) -> None:
    record = svc.start(spec).record
    claimed = svc.ServiceSpec(**{**spec.__dict__, "project_id": "imposter"})
    svc.write_record(
        spec.paths, svc.EndpointRecord(**{**record.payload(), "project_id": "imposter"})
    )

    with pytest.raises(svc.ServiceUnavailable, match="identity mismatch") as failure:
        svc.resolve(claimed)
    assert failure.value.state == "mismatch"


def test_process_that_exits_during_startup_is_reported(spec: svc.ServiceSpec) -> None:
    context = dict(_context(), _exit_immediately=True)
    (spec.repo_root / "fake-context.json").write_text(json.dumps(context))

    with pytest.raises(svc.ServiceStartFailed, match="exited during startup"):
        svc.start(spec)
    assert not spec.paths.record.exists()


def test_token_file_open_to_others_is_refused(spec: svc.ServiceSpec) -> None:
    svc.ensure_token(spec.paths)
    spec.paths.token.chmod(0o644)

    with pytest.raises(PermissionError, match="chmod 600"):
        svc.start(spec)


def test_host_daemon_owned_service_is_not_stopped_behind_its_back(
    spec: svc.ServiceSpec,
) -> None:
    record = svc.start(spec).record
    owned = svc.EndpointRecord(
        **{**record.payload(), "supervisor": "host-daemon", "supervisor_pid": os.getpid()}
    )
    svc.write_record(spec.paths, owned)

    with pytest.raises(svc.ServiceOwned, match="host-daemon"):
        svc.stop(spec)
    assert svc.process_alive(record.pid)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_supervisor_restarts_with_bounded_backoff_then_gives_up(spec: svc.ServiceSpec) -> None:
    clock = _Clock()
    supervisor = svc.ServiceSupervisor(
        spec, kind="host-daemon", backoff_initial=1.0, max_failures=3, clock=clock
    )

    first = supervisor.tick()
    assert first.state == "running"
    assert first.record is not None and first.record.supervisor == "host-daemon"
    os.kill(first.record.pid, signal.SIGKILL)
    _wait_dead(first.record.pid)

    crashed = supervisor.tick()
    assert (crashed.state, crashed.failures) == ("backoff", 1)
    assert svc.status(spec).state == "stale"
    assert supervisor.tick().state == "backoff"  # still inside the 1s backoff window

    clock.now += 1.0
    second = supervisor.tick()
    assert second.state == "running"
    assert second.record is not None and second.record.pid != first.record.pid

    os.kill(second.record.pid, signal.SIGKILL)
    _wait_dead(second.record.pid)
    assert supervisor.tick().failures == 2
    clock.now += 2.0  # exponential: 1s, then 2s
    third = supervisor.tick()
    assert third.state == "running"
    assert third.record is not None
    os.kill(third.record.pid, signal.SIGKILL)
    _wait_dead(third.record.pid)

    gave_up = supervisor.tick()
    assert gave_up.state == "failed"
    clock.now += 100.0
    assert supervisor.tick().state == "failed"
    with pytest.raises(svc.ServiceUnavailable, match="stale"):
        svc.resolve(spec)


def test_supervisor_shutdown_sigterms_child_and_retracts_record(spec: svc.ServiceSpec) -> None:
    supervisor = svc.ServiceSupervisor(spec, kind="foreground")
    record = supervisor.tick().record
    assert record is not None

    supervisor.shutdown()

    _wait_dead(record.pid)
    assert not svc.process_alive(record.pid)
    assert not spec.paths.record.exists()
    assert supervisor.tick().state == "stopped"


def test_supervisor_adopts_an_already_verified_service(spec: svc.ServiceSpec) -> None:
    detached = svc.start(spec).record
    supervisor = svc.ServiceSupervisor(spec, kind="host-daemon")

    adopted = supervisor.tick()

    assert adopted.record is not None and adopted.record.pid == detached.pid
    supervisor.shutdown()
    assert svc.process_alive(detached.pid)  # not its child: left to its own owner
