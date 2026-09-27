"""Opt-in: ``work.claim-next`` is exclusive under real contention, against a disposable scratch
hive this test creates itself — never a managed hive.

The CLI-compatibility pick/claim/re-verify loop (:mod:`beadhive.work_next`'s ``eligible`` /
``claim_won`` / ``decline``) exists only because ``bd update --claim`` is not a hard
compare-and-swap; two real drivers racing the same bead can both exit 0, and the caller learns
who actually won only by re-reading. ``work.claim-next`` replaces that with one atomic HTTP
transaction. This proves that guarantee against a real, disposable, embedded-Dolt Beads 1.3.0
service (the same ``bd serve`` mechanism ``test_real_v13_local_supervision`` in
``beadhive-beads-client`` uses) — several real OS threads racing ONE ready bead, exactly one of
which comes back with it claimed.

Opt-in via ``BEADS_QUEUE_SCRATCH=1``, the same "explicit env var, self-skip otherwise" convention
every other ``real_service`` test in this workspace follows (see
``test_core_routing_real_service.py``'s module docstring for why this is opt-in even though it
provisions its own scratch hive)::

    BEADS_QUEUE_SCRATCH=1 \
    uv run --locked --all-packages pytest packages/beadhive-core/tests -m real_service
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, LocalEndpoint, RemoteEndpoint
from beadhive_core import QUEUE_CAPABILITIES, QueueCommands

pytestmark = pytest.mark.real_service


def _bd(*args: str, cwd: Path) -> dict:
    result = subprocess.run(
        ["bd", *args, "--json"], cwd=cwd, check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _init_scratch_hive(path: Path) -> None:
    """An OWNED-mode (`bd init --server`) scratch hive: `bd serve` refuses embedded Dolt
    ("bd serve requires a Dolt SQL server"), so the default `bd init` shape this cohort's other
    scratch-hive tests use (`test_core_routing_real_service.py`) cannot serve HTTP at all. This
    mirrors `tests/test_coordination_int.py`'s `server_store` fixture instead."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "qsc", "--non-interactive"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _reap_dolt_server(beads_dir: Path) -> None:
    """Terminate the owned-mode Dolt sql-server `bd init --server` started, via its pidfile —
    the same discipline `tests/harness/world.py`'s `reap_dolt_server` uses, kept local to this
    package's own test tree rather than importing across the package boundary."""
    pid_file = beads_dir / "dolt-server.pid"
    if not pid_file.is_file():
        return
    with contextlib.suppress(OSError, ValueError):
        pid = int(pid_file.read_text().strip())
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                time.sleep(0.1)
                os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def test_claim_next_is_exclusive_under_real_contention(tmp_path: Path) -> None:
    if not os.environ.get("BEADS_QUEUE_SCRATCH"):
        pytest.skip("set BEADS_QUEUE_SCRATCH=1 to run the real-bd scratch-hive proof")

    _init_scratch_hive(tmp_path)
    try:
        context = _bd("context", cwd=tmp_path)
        expected = ExpectedContext(
            context["project_id"],
            context["database"],
            required_capabilities=QUEUE_CAPABILITIES,
            repo_root=tmp_path,
        )
        bead = _bd(
            "create",
            "--title",
            "contended bead",
            "--type",
            "task",
            "--priority",
            "2",
            cwd=tmp_path,
        )
        bead_id = bead["id"]

        port = _free_port()
        commands = QueueCommands()
        racer_count = 4
        barrier = Barrier(racer_count)
        url = f"http://127.0.0.1:{port}"

        def contest(actor: str) -> dict | None:
            with BeadsSession(RemoteEndpoint(url), expected) as claimant:
                barrier.wait(timeout=10)  # every racer arrives before any of them claims
                return commands.claim_next(claimant, actor).claimed

        with BeadsSession(LocalEndpoint(tmp_path, port=port), expected):
            with ThreadPoolExecutor(max_workers=racer_count) as pool:
                outcomes = list(pool.map(contest, [f"dev/racer-{i}" for i in range(racer_count)]))

        won = [outcome for outcome in outcomes if outcome is not None]
        assert len(won) == 1, f"exactly one racer may win the only ready bead, got {outcomes}"
        assert won[0]["id"] == bead_id
        lost = [outcome for outcome in outcomes if outcome is None]
        assert len(lost) == racer_count - 1

        shown = _bd("show", bead_id, cwd=tmp_path)
        final = shown[0] if isinstance(shown, list) else shown
        assert final["assignee"] == won[0]["assignee"]  # the store agrees with the believed winner
        assert final["status"] == "in_progress"
    finally:
        _reap_dolt_server(tmp_path / ".beads")
