"""Hermetic real-process validation for host daemon to installed Frame Bridge."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "harness" / "frame_bridge_live.py"

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


@pytest.mark.skipif(sys.platform != "linux", reason="fixed listeners require a network namespace")
def test_authenticated_daemon_to_installed_frame_bridge_real_process_path(tmp_path: Path) -> None:
    if shutil.which("unshare") is None or shutil.which("ip") is None:
        pytest.skip("network namespace tools are unavailable")
    completed = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            sys.executable,
            str(HARNESS),
            str(tmp_path),
            str(Path(sys.executable).parent),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == {
        "status": "ok",
        "contractVersion": "gateway.v1",
        "coverage": [
            "SSE delivery and reconnect",
            "authenticated discovery",
            "credential failure",
            "health",
            "process/socket/credential/temp-state cleanup",
            "redacted snapshot",
            "revision-checked refresh",
            "secret redaction",
        ],
    }
