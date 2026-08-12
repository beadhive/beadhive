"""Integration: `dolt_health.probe_endpoint` against REAL, live, query-answering servers
(bh-areg.3) — not a synthetic handshake byte string.

This is the test the reviewer asked for after finding `probe_endpoint` read byte 0 of the wire
instead of byte 4 (the MySQL packet header is 4 bytes; the protocol-version byte is the first
byte of the *payload*, not the first byte on the wire). A unit test with a corrected
``_FakeServer`` (see ``test_dolt_health.py``) is necessary but not sufficient to catch that
class of bug — it proves the probe agrees with itself, not with a real server. This module
proves the probe agrees with reality, against BOTH shapes the ADR names:

  - a standalone EXTERNAL `dolt sql-server`, spawned directly (not through `bd`) — mirrors
    bh-u562.1 finding 9's exact scenario.
  - bd's own SHARED server (mode (a), the fleet's actual chosen target per
    ``docs/design/dolt-server-mode-adr.md``), spawned via `bd init --shared-server`.

Both were independently confirmed unreachable=False against the pre-fix probe at review time;
this test locks in reachable=True against the fixed one.

Never touches the operator's real ``~/.beads/shared-server/`` or any registered hive: the
shared-server case runs under ``BEADS_SHARED_SERVER_DIR`` pointed at ``tmp_path``, and a
non-default ``BEADS_DOLT_SERVER_PORT`` so it can never collide with a real fleet server
listening on the default 3308. The externally-spawned server is torn down in a ``finally``; bd's
shared server is torn down by the ``isolated_shared_server`` fixture's finalizer, which kills it
by pidfile (``harness.world.reap_dolt_server``) rather than asking ``bd dolt stop`` — a call that
refuses in some dolt-mode states and, with ``check=False``, left the server running (bh-5mc8g).

Marked ``integration`` (slower — spins up real Dolt sql-servers) + self-skips without a ``bd``
binary on PATH, per this repo's marker convention.
"""

from __future__ import annotations

import socket
import subprocess
import time

import pytest

from beadhive import dolt_health
from beadhive.run import run
from harness.beads import skip_if_no_bd
from harness.world import reap_dolt_server

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_STARTUP_TIMEOUT = 30.0  # seconds to wait for a freshly spawned server to accept connections
_INIT_TIMEOUT = 60


def _free_port() -> int:
    """Bind an OS-assigned ephemeral port, then release it. There's an inherent, small,
    accepted TOCTOU race between release and the real server binding it — the same tradeoff
    the unit suite's own ``_free_port`` makes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_until_accepting(host: str, port: int, *, timeout: float) -> None:
    """Poll with a raw TCP connect (deliberately NOT `dolt_health.probe_endpoint` — this is
    startup-readiness plumbing, not the assertion under test) until *host*:*port* accepts a
    connection, or raise."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.2)
    raise TimeoutError(f"{host}:{port} never started accepting connections: {last_err}")


def test_probe_endpoint_reachable_against_a_real_standalone_external_dolt_server(tmp_path):
    """bh-u562.1 finding 9's exact scenario: a standalone `dolt sql-server`, spawned directly
    (bd never touches it — genuinely external), answering real queries."""
    data_dir = tmp_path / "external-data"
    data_dir.mkdir()
    port = _free_port()
    proc = subprocess.Popen(
        [
            "dolt",
            "sql-server",
            "--host=127.0.0.1",
            f"--port={port}",
            f"--data-dir={data_dir}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_accepting("127.0.0.1", port, timeout=_STARTUP_TIMEOUT)

        result = dolt_health.probe_endpoint("127.0.0.1", port, timeout=5.0)

        assert result.reachable is True, result.detail
        assert f"127.0.0.1:{port}" in result.detail
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def isolated_shared_server(tmp_path, monkeypatch):
    """This test's OWN shared-server instance — its own `BEADS_SHARED_SERVER_DIR` and a fresh
    ephemeral port, never the operator's real `~/.beads/shared-server/` nor the default 3308 a
    genuine fleet server might hold. Yields ``(project_dir, port)``.

    The teardown is `harness.world.reap_dolt_server`, matching `test_hub_bulk_int.py` /
    `test_onboard_server_mode_int.py`, and NOT the `bd … dolt stop` with `check=False` this test
    used to run (bh-5mc8g). `bd dolt stop` refuses in some dolt-mode states and the swallowed
    refusal left a real sql-server running against a tmp dir pytest had already deleted — observed
    orphaned (reparented to PID 1) on the operator's machine, from this exact test. Killing by the
    pidfile under this test's own dir cannot name anything but this test's server."""
    shared_dir = tmp_path / "shared-server-dir"
    project_dir = tmp_path / "shared-hive"
    project_dir.mkdir()
    port = _free_port()

    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(shared_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(port))
    yield project_dir, port
    reap_dolt_server(shared_dir)


def test_probe_endpoint_reachable_against_bds_real_shared_server(isolated_shared_server):
    """Mode (a) — bd's shared server, the fleet's actual chosen target
    (docs/design/dolt-server-mode-adr.md). Isolated from the real fleet entirely by the
    `isolated_shared_server` fixture, which also owns the teardown."""
    project_dir, port = isolated_shared_server

    run(
        ["bd", "init", "--shared-server", "--prefix", "areg3probe", "--non-interactive"],
        cwd=str(project_dir),
        check=True,
        capture=True,
        timeout=_INIT_TIMEOUT,
    )
    _wait_until_accepting("127.0.0.1", port, timeout=_STARTUP_TIMEOUT)

    # Prove it's genuinely live and query-answering, not just accepting TCP — the exact
    # distinction bh-u562.1 finding 9 turned on (bd reported a live server as down).
    res = run(
        ["bd", "-C", str(project_dir), "q", "areg3 probe bead"],
        check=True,
        capture=True,
    )
    bead_id = (res.stdout or "").strip().splitlines()[-1].strip()
    assert bead_id.startswith("areg3probe-")

    result = dolt_health.probe_endpoint("127.0.0.1", port, timeout=5.0)

    assert result.reachable is True, result.detail
    assert f"127.0.0.1:{port}" in result.detail
