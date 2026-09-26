"""`bh host beads` and the host runtime's supervised Beads API service owner (bh-bwnys.4).

The service contract itself (record, readiness, backoff) is proven in the Beads client package
(`test_service_supervision.py`). These tests prove the Beadhive binding: hive identity and
embedded-Dolt refusal, the CLI verbs over a loopback stand-in `bd`, the consumer resolver's
fail-closed message, and host-daemon intent reconciliation. No network beyond loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import host_beads, registry
from beadhive.cli import app

runner = CliRunner()

# Answers only the negotiation routes on 127.0.0.1, with the bd serve process contract (argv,
# bearer token file, SIGTERM). It is not a Beads emulation.
_STAND_IN_BD = r"""
import json, os, signal, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
repo = args[args.index("-C") + 1]
host, port = args[args.index("--addr") + 1].rsplit(":", 1)
token_file = args[args.index("--auth-token-file") + 1]
meta = json.load(open(os.path.join(repo, ".beads", "metadata.json")))
context = {
    "api_version": "v0", "bd_version": "1.3.0", "schema_version": 1, "backend": "dolt",
    "dolt_mode": "server", "database": meta["dolt_server_database"],
    "project_id": meta["project_id"],
    "capabilities": ["project.enforce", "issues.get", "issues.list", "ready.list"],
}


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
        tokens = {line.strip() for line in open(token_file) if line.strip()}
        if self.headers.get("Authorization", "")[7:] not in tokens:
            return self.reply(401, {"status": 401, "title": "Unauthorized",
                                    "code": "unauthorized", "request_id": "r"})
        if path == "/v0/beads/context":
            return self.reply(200, context)
        return self.reply(200, {"items": [], "has_more": False})


server = ThreadingHTTPServer((host, int(port)), Handler)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
server.serve_forever()
"""

HIVE = "github/acme/widgets"


def _hive(tmp_path: Path, *, mode: str = "server") -> Path:
    main = tmp_path / "workspace" / HIVE
    (main / ".beads").mkdir(parents=True)
    (main / ".beads" / "metadata.json").write_text(
        json.dumps(
            {
                "dolt_mode": mode,
                "dolt_database": "beads",
                "dolt_server_database": "acme_w",
                "project_id": "proj-acme",
            }
        )
    )
    return main


@pytest.fixture
def stand_in_bd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bd = bin_dir / "bd"
    bd.write_text(f"#!{sys.executable}\n{_STAND_IN_BD}")
    bd.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bd


@pytest.fixture
def served_hive(tmp_path: Path, stand_in_bd: Path, monkeypatch: pytest.MonkeyPatch):
    main = _hive(tmp_path)
    monkeypatch.setattr(host_beads, "locate", lambda _cfg, _hive="": (main, HIVE))
    monkeypatch.setattr(registry, "workspace_root", lambda: tmp_path / "workspace")
    yield main
    record, _ = host_beads.service_module().load_record(host_beads.service_spec(main, HIVE).paths)
    if record is not None:
        try:
            os.kill(record.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_spec_binds_hive_identity_and_private_runtime_dir(tmp_path, stand_in_bd):
    main = _hive(tmp_path)

    spec = host_beads.service_spec(main, HIVE, root=tmp_path / "run")

    assert (spec.workspace, spec.project_id, spec.database) == (HIVE, "proj-acme", "acme_w")
    assert spec.repo_root == main
    assert spec.paths.directory == tmp_path / "run" / "github-acme-widgets"
    assert spec.start_command == f"bh host beads start --hive {HIVE}"
    assert host_beads.runtime_root(tmp_path) == tmp_path / "run" / "beads-serve"


def test_embedded_dolt_hive_is_refused_with_a_clear_message(tmp_path, stand_in_bd):
    main = _hive(tmp_path, mode="embedded")

    with pytest.raises(host_beads.HiveNotServable, match="embedded Dolt.*server mode"):
        host_beads.service_spec(main, HIVE)


def test_cli_start_is_idempotent_status_verifies_and_stop_retracts(served_hive):
    first = runner.invoke(app, ["host", "beads", "start", "--json"])
    assert first.exit_code == 0, first.output
    started = json.loads(first.stdout)
    assert started["started"] is True
    record = started["record"]
    assert record["address"] == "127.0.0.1"
    assert record["database"] == "acme_w"
    token = Path(record["token_file"]).read_text().strip()
    assert token not in first.stdout

    again = runner.invoke(app, ["host", "beads", "start", "--json"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["started"] is False
    assert json.loads(again.stdout)["record"]["pid"] == record["pid"]

    status = runner.invoke(app, ["host", "beads", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["state"] == "running"

    listed = runner.invoke(app, ["host", "beads", "status", "--all", "--json"])
    assert [row["hive"] for row in json.loads(listed.stdout)["hives"]] == [HIVE]

    stopped = runner.invoke(app, ["host", "beads", "stop"])
    assert stopped.exit_code == 0, stopped.output
    assert f"stopped bd serve pid {record['pid']}" in stopped.stdout

    after = runner.invoke(app, ["host", "beads", "status"])
    assert after.exit_code == 1
    assert "absent" in after.stdout
    assert f"start it with `bh host beads start --hive {HIVE}`" in after.stdout


def test_resolver_fails_closed_until_started_then_returns_loopback_endpoint(served_hive):
    entry = {"provider": "github", "org": "acme", "repo": "widgets"}
    service = host_beads.service_module()
    spec = host_beads.service_spec(served_hive, HIVE)

    with pytest.raises(service.ServiceUnavailable) as absent:
        host_beads.resolve_endpoint(served_hive, entry=entry, cfg={})
    assert f"start it with `bh host beads start --hive {HIVE}`" in str(absent.value)

    record = service.start(spec).record
    try:
        endpoint = host_beads.resolve_endpoint(served_hive, entry=entry)
        session = host_beads.resolve_session(served_hive, entry=entry)
    finally:
        service.stop(spec)
    assert endpoint.url == f"http://127.0.0.1:{record.port}"
    assert session.expected.project_id == "proj-acme"
    assert session.expected.database == "acme_w"
    assert session.context is None  # handed over unopened; the caller opens (and re-verifies)


def test_stop_refuses_a_daemon_supervised_service(served_hive):
    service = host_beads.service_module()
    spec = host_beads.service_spec(served_hive, HIVE)
    record = service.start(spec).record
    service.write_record(
        spec.paths,
        service.EndpointRecord(
            **{**record.payload(), "supervisor": "host-daemon", "supervisor_pid": os.getpid()}
        ),
    )

    refused = runner.invoke(app, ["host", "beads", "stop"])

    assert refused.exit_code == 1
    assert f"release it with `bh host beads disable --hive {HIVE}`" in refused.output


class _FakeSupervisor:
    def __init__(self, spec) -> None:
        self.spec = spec
        self.ticks = 0
        self.stopped = False

    def tick(self) -> None:
        self.ticks += 1

    def shutdown(self) -> None:
        self.stopped = True


def _supervision(tmp_path: Path, made: list[_FakeSupervisor]) -> host_beads.DaemonBeadsSupervision:
    def factory(spec):
        made.append(_FakeSupervisor(spec))
        return made[-1]

    return host_beads.DaemonBeadsSupervision(
        tmp_path / "run", interval=0.01, supervisor_factory=factory
    )


def test_daemon_supervises_enabled_hives_and_releases_disabled_ones(tmp_path, stand_in_bd):
    main = _hive(tmp_path)
    spec = host_beads.service_spec(main, HIVE, root=tmp_path / "run")
    made: list[_FakeSupervisor] = []
    supervision = _supervision(tmp_path, made)

    supervision.reconcile()
    assert made == []

    host_beads.enable(spec)
    assert host_beads.is_enabled(spec)
    supervision.reconcile()
    supervision.reconcile()
    assert len(made) == 1 and made[0].ticks == 2
    assert made[0].spec.workspace == HIVE

    assert host_beads.disable(spec) is True
    supervision.reconcile()
    assert made[0].stopped is True
    assert supervision.supervisors == {}


def test_daemon_records_an_unservable_intent_without_supervising_it(tmp_path, stand_in_bd):
    main = _hive(tmp_path)
    spec = host_beads.service_spec(main, HIVE, root=tmp_path / "run")
    host_beads.enable(spec)
    (main / ".beads" / "metadata.json").write_text(json.dumps({"dolt_mode": "embedded"}))
    made: list[_FakeSupervisor] = []
    supervision = _supervision(tmp_path, made)

    supervision.reconcile()

    assert made == []
    assert "embedded Dolt" in supervision.errors[HIVE]


def test_daemon_component_shuts_supervisors_down_on_drain(tmp_path, stand_in_bd):
    main = _hive(tmp_path)
    host_beads.enable(host_beads.service_spec(main, HIVE, root=tmp_path / "run"))
    made: list[_FakeSupervisor] = []
    supervision = _supervision(tmp_path, made)
    component = supervision.component()

    async def exercise() -> None:
        async with component.lifespan(None):
            for _ in range(200):
                if made and made[0].ticks:
                    break
                await asyncio.sleep(0.01)

    asyncio.run(exercise())

    assert component.name == "beads-service-supervision"
    assert made and made[0].ticks >= 1
    assert made[0].stopped is True
