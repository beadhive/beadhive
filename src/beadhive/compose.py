"""Shared container-compose lifecycle for the local stacks (dolt SQL server, otel-lgtm).

Both stacks drive the same container runtime — backend selected by the shared ``dolt.backend``
config key, same compose binary — and differ only in WHICH compose file + bundled template they
seed and run. This module owns the duplicated lifecycle (backend selection, compose-binary
resolution, the daemon pre-step, the ``~/.ws/.env`` overlay, file seeding, and the
``compose -f <file> <args>`` invocation), so ``dolt.py`` and ``otel_lgtm.py`` are thin wrappers.
Crucially both stacks now run compose with the ``.env`` overlay applied (previously only dolt did).
"""

from __future__ import annotations

import os
import shutil

from . import config
from .run import ok, run


def backend() -> str:
    return str(config.dolt_cfg().get("backend", "colima"))


def compose_cmd(backend):
    override = config.dolt_cfg().get("compose")
    if override:
        return override.split() if isinstance(override, str) else list(override)
    if backend == "podman":
        return ["podman", "compose"]
    if ok(["docker", "compose", "version"]):
        return ["docker", "compose"]
    return ["docker-compose"]


def ensure_up(backend):
    """Backend-specific pre-step to get a container daemon running."""
    if backend == "colima":
        if not ok(["colima", "status"]):
            run(["colima", "start"])
    elif backend == "podman":
        run(["podman", "machine", "start"], check=False)
    # docker / none: assume the daemon is already running / managed elsewhere


def read_env():
    """os.environ layered with ~/.ws/.env (KEY=VALUE lines)."""
    env = dict(os.environ)
    envfile = config.env_file()
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def run_compose(backend, compose_file, template, *args):
    """Seed ``compose_file`` from bundled ``template`` if absent, then run
    ``compose -f <compose_file> <args>`` from the ws home with the ``~/.ws/.env`` overlay applied
    (so BOTH stacks see the DOLT_*/token/port values the env file defines)."""
    if not compose_file.exists():
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.template(template), compose_file)
    cmd = compose_cmd(backend) + ["-f", str(compose_file), *args]
    run(cmd, cwd=str(config.home()), env=read_env())
