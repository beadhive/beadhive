"""Manage the grafana/otel-lgtm local observability stack via compose.

Mirrors the dolt compose pattern: the bundled template is seeded to
~/.ws/docker-compose.otel.yml on first use, then driven via compose up/down/logs/ps.
One command brings up Grafana (port 3000) with OTLP collectors for traces, metrics,
and logs (gRPC 4317 / HTTP 4318).

Backend selection (colima | docker | podman | none) and the compose binary override
are shared with the dolt config key ``dolt.backend`` — same container runtime on the
machine, different service.
"""

from __future__ import annotations

import shutil

from . import config
from .run import ok, run


def _backend() -> str:
    return str(config.dolt_cfg().get("backend", "colima"))


def _compose_cmd(backend):
    override = config.dolt_cfg().get("compose")
    if override:
        return override.split() if isinstance(override, str) else list(override)
    if backend == "podman":
        return ["podman", "compose"]
    if ok(["docker", "compose", "version"]):
        return ["docker", "compose"]
    return ["docker-compose"]


def _ensure_up(backend):
    """Backend-specific pre-step to get a container daemon running."""
    if backend == "colima":
        if not ok(["colima", "status"]):
            run(["colima", "start"])
    elif backend == "podman":
        run(["podman", "machine", "start"], check=False)
    # docker / none: assume daemon is already running / managed elsewhere


def _ensure_compose_file():
    """Seed ~/.ws/docker-compose.otel.yml from the bundled template if absent."""
    target = config.otel_compose_file()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.template("docker-compose.otel.yml"), target)


def _compose(backend, *args):
    _ensure_compose_file()
    cmd = _compose_cmd(backend) + ["-f", str(config.otel_compose_file()), *args]
    run(cmd, cwd=str(config.home()))


def up():
    backend = _backend()
    _ensure_up(backend)
    _compose(backend, "up", "-d")


def down():
    _compose(_backend(), "down")


def logs():
    _compose(_backend(), "logs", "-f")


def ps():
    _compose(_backend(), "ps")
