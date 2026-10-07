"""Integration (bh-ijxif): the watchdog and the read-only DOLT_ROOT_PATH against PRIVATE scratch
Dolt servers. Every server has its own port, data dir, HOME and DOLT_ROOT_PATH under tmp_path;
nothing touches a live server or ~/.dolt. Self-skips without a dolt binary."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.world import free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "dolt_globals_watchdog_int", ROOT / "scripts" / "dolt_globals_watchdog.py"
)
assert _SPEC is not None and _SPEC.loader is not None
W = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = W
_SPEC.loader.exec_module(W)

CFG = {
    "user.name": "fixture",
    "user.email": "fixture@fixture.invalid",
    "versioncheck.disabled": "true",
    "metrics.disabled": "true",
}


class Scratch:
    def __init__(self, base: Path, *, read_only_root: bool):
        self.base = base
        self.port = free_port()
        self.home = base / "home"
        self.data = base / "data"
        self.root = base / "server-root"
        self.client_root = base / "client-root"
        for d in (
            self.home,
            self.data,
            self.root / ".dolt" / "eventsData",
            self.client_root / ".dolt",
        ):
            d.mkdir(parents=True, exist_ok=True)
        for r in (self.root, self.client_root):
            (r / ".dolt" / "config_global.json").write_text(json.dumps(CFG))
        (base / "server.yaml").write_text(
            f"data_dir: {self.data}\nlistener:\n  host: 127.0.0.1\n  port: {self.port}\n"
            "  max_connections: 100\n"
        )
        self.read_only_root = read_only_root
        if read_only_root:
            (self.root / ".dolt" / "config_global.json").chmod(0o444)
            (self.root / ".dolt").chmod(0o555)
        self.proc: subprocess.Popen | None = None

    def env(self, root: Path, *, client: bool = False) -> dict[str, str]:
        env = {**os.environ, "DOLT_ROOT_PATH": str(root), "HOME": str(self.home)}
        if client:
            env["DOLT_CLI_PASSWORD"] = ""  # root has no password; stops the interactive prompt
        else:
            env.pop("DOLT_CLI_PASSWORD", None)
        return env

    def start(self) -> None:
        log = (self.base / "server.log").open("a")
        self.proc = subprocess.Popen(
            [shutil.which("dolt"), "sql-server", "--config", str(self.base / "server.yaml")],
            cwd=self.base,
            env=self.env(self.root),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        end = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > end:
                    raise AssertionError("scratch dolt server failed to start") from None
                time.sleep(0.1)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)

    def sql(self, statement: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                shutil.which("dolt"),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--user",
                "root",
                "--no-tls",
                "sql",
                "-r",
                "json",
                "-q",
                statement,
            ],
            env=self.env(self.client_root, client=True),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def value(self, name: str) -> str:
        out = self.sql(f"SELECT @@GLOBAL.{name} AS v")
        return str(json.loads(out.stdout)["rows"][0]["v"])

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dolt_globals_watchdog.py"), *args],
            env=self.env(self.client_root, client=True),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def target(self) -> list[str]:
        return ["--host", "127.0.0.1", "--port", str(self.port), "--user", "root"]

    def close(self) -> None:
        self.stop()
        if self.read_only_root:
            (self.root / ".dolt").chmod(0o755)


@pytest.fixture
def scratch_factory(tmp_path):
    if shutil.which("dolt") is None:
        pytest.skip("dolt binary unavailable")
    made: list[Scratch] = []

    def make(name: str, *, read_only_root: bool) -> Scratch:
        s = Scratch(tmp_path / name, read_only_root=read_only_root)
        made.append(s)
        s.start()
        return s

    yield make
    for s in made:
        s.close()


def test_watchdog_flags_a_forced_global_and_does_not_fix_it(scratch_factory):
    s = scratch_factory("w", read_only_root=True)
    ok = s.cli("check", *s.target())
    assert ok.returncode == 0, ok.stderr + ok.stdout

    assert s.sql("SET GLOBAL dolt_force_transaction_commit = 1").returncode == 0
    drift = s.cli("check", *s.target())
    assert drift.returncode == 2
    assert "ALERT" in drift.stderr and "dolt_force_transaction_commit" in drift.stderr
    # Never fixed silently: the forced value is still there after the watchdog ran.
    assert s.value("dolt_force_transaction_commit") == "1"

    # Policy is config: expecting 1 makes the same server pass without touching it.
    tolerated = s.cli("check", *s.target(), "--expect", "dolt_force_transaction_commit=1")
    assert tolerated.returncode == 0


def test_unreachable_server_is_unverifiable(scratch_factory):
    s = scratch_factory("u", read_only_root=True)
    s.stop()
    out = s.cli("check", *s.target())
    assert out.returncode == 3 and "ALERT" in out.stderr


def test_read_only_root_refuses_persist_and_nothing_survives_restart(scratch_factory):
    s = scratch_factory("ro", read_only_root=True)
    assert s.cli("persist", *s.target()).returncode == 0  # refusal is the pass condition
    refused = s.sql("SET PERSIST max_connections = 777")
    assert refused.returncode != 0 and "permission denied" in (refused.stderr + refused.stdout)
    assert "sqlserver.global" not in (s.root / ".dolt" / "config_global.json").read_text()
    s.stop()
    s.start()
    assert s.value("max_connections") != "777"
    if os.geteuid() != 0:
        assert s.cli("root-check", "--root", str(s.root)).returncode == 0


def test_writable_root_lets_persist_survive_restart_and_watchdog_alerts(scratch_factory):
    s = scratch_factory("rw", read_only_root=False)
    probe = s.cli("persist", *s.target())
    assert probe.returncode == 2 and "SET PERSIST SUCCEEDED" in probe.stderr
    assert s.sql("SET PERSIST max_connections = 777").returncode == 0
    s.stop()
    s.start()
    assert s.value("max_connections") == "777"  # the exposure the read-only root closes
    assert s.cli("root-check", "--root", str(s.root)).returncode == 2
