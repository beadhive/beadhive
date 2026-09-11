"""Exact minimum-version OTel compatibility proof (explicit, not ordinary pytest).

Run ``just otel-minimum-check``. The parent creates a fresh environment and installs the project
with the declared minimum SDK/exporter; the child exercises real daemon initialization and a dead
collector. Keeping environment creation outside ordinary pytest means a cold package cache cannot
make the hermetic unit/full suites fail for an unrelated network reason.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from importlib.metadata import version
from pathlib import Path

_MINIMUM = "1.37.0"
_CHILD_ENV = "BH_OTEL_MINIMUM_PROOF_CHILD"


async def _exercise_child() -> dict:
    from beadhive import otel
    from beadhive.daemon_telemetry import DaemonTelemetry

    if version("opentelemetry-sdk") != _MINIMUM:
        raise RuntimeError("minimum proof did not install the exact SDK version")
    if version("opentelemetry-exporter-otlp") != _MINIMUM:
        raise RuntimeError("minimum proof did not install the exact exporter version")
    if os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT") != "0.5":
        raise RuntimeError("minimum proof requires the fractional exporter timeout")

    telemetry = DaemonTelemetry(
        cfg={
            "otel": {
                "enabled": True,
                "protocol": "http/protobuf",
                "endpoint": "http://127.0.0.1:1",
                "export_timeout_seconds": 0.5,
                "flush_timeout_seconds": 2.0,
            }
        },
        host_id="minimum-version-host",
        instance_id="minimum-version-instance",
        flush_budget_seconds=2.0,
    )
    if not await telemetry.start():
        raise RuntimeError("minimum SDK daemon telemetry did not initialize")
    started = time.monotonic()
    await telemetry.stop()
    return {
        "active": otel.is_active(),
        "elapsed": time.monotonic() - started,
        "workers": [
            thread.name for thread in threading.enumerate() if thread is not threading.main_thread()
        ],
    }


def _child() -> None:
    payload = asyncio.run(_exercise_child())
    passed = not payload["active"] and payload["elapsed"] < 2.5 and not payload["workers"]
    print(json.dumps(payload), flush=True)
    # A leaking non-daemon SDK worker must fail promptly rather than hang this proof at interpreter
    # exit. Worker state has already been captured in the payload the parent validates.
    os._exit(0 if passed else 1)


def _parent() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("the minimum OTel proof requires uv")
    project_root = Path(__file__).parents[2]
    declared = set(
        tomllib.loads((project_root / "pyproject.toml").read_text())["project"][
            "optional-dependencies"
        ]["otel"]
    )
    floor = _MINIMUM.rsplit(".", 1)[0]
    for package in ("opentelemetry-sdk", "opentelemetry-exporter-otlp"):
        if f"{package}>={floor},<2" not in declared:
            raise RuntimeError(f"{package} declared floor does not match proof {_MINIMUM}")
    with tempfile.TemporaryDirectory(prefix="bh-otel-minimum-") as temp_dir:
        python = Path(temp_dir) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([uv, "venv", "--python", sys.executable, temp_dir], check=True)
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--editable",
                str(project_root),
                f"opentelemetry-sdk=={_MINIMUM}",
                f"opentelemetry-exporter-otlp=={_MINIMUM}",
            ],
            check=True,
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("OTEL_")}
        env[_CHILD_ENV] = "1"
        env["OTEL_EXPORTER_OTLP_TIMEOUT"] = "0.5"
        completed = subprocess.run(
            [str(python), str(Path(__file__).resolve())],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "minimum OTel proof failed")
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    if payload["active"] or payload["elapsed"] >= 2.5 or payload["workers"]:
        raise RuntimeError(f"minimum OTel proof violated shutdown contract: {payload}")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    if os.environ.get(_CHILD_ENV) == "1":
        _child()
    else:
        _parent()
