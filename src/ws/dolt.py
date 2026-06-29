"""Provision and manage the Dolt SQL server behind beads.

Ports scripts/provision.sh + the justfile up/down/logs/ps/sql recipes, with a thin
pluggable container backend (colima | docker | podman | none) selected by
`dolt.backend` in config — start at one real backend with a clean seam, not a
plugin framework.
"""

from __future__ import annotations

import os
import shutil
import time

import typer

from . import config
from .run import ok, run

# The Dolt image creates the app user AFTER it starts accepting connections, so we
# poll for the user (not just the server). 60×1s covers a cold image start; bump if
# your host is slower.
WAIT_TRIES = 60
WAIT_SLEEP = 1


def _backend() -> str:
    return str(config.dolt_cfg().get("backend", "colima"))


def _runtime(backend) -> str:
    return "podman" if backend == "podman" else "docker"


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
    # docker / none: assume the daemon is already running / managed elsewhere


def _read_env():
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


def _ensure_compose_file():
    """Seed ~/.ws/docker-compose.yml from the bundled template if absent."""
    target = config.compose_file()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.template("docker-compose.yml"), target)


def _compose(backend, *args):
    _ensure_compose_file()
    cmd = _compose_cmd(backend) + ["-f", str(config.compose_file()), *args]
    run(cmd, cwd=str(config.home()), env=_read_env())


def provision():
    env = _read_env()
    host = env.get("DOLT_HOST", "127.0.0.1")
    port = env.get("DOLT_PORT", "3307")
    app = env.get("DOLT_USER", "beads")
    app_pw = env.get("BEADS_DOLT_PASSWORD", "")
    root_pw = env.get("DOLT_ROOT_PASSWORD", "")

    typer.echo(f"waiting for app user '{app}' on {host}:{port} ...")
    base = ["dolt", "--host", host, "--port", port, "--no-tls"]
    ready = False
    for _ in range(WAIT_TRIES):
        if ok([*base, "--user", app, "--password", app_pw, "sql", "-q", "select 1"], env=env):
            ready = True
            break
        time.sleep(WAIT_SLEEP)
    if not ready:
        typer.echo(f"✗ app user not ready on {host}:{port}", err=True)
        raise typer.Exit(1)

    grant = f"GRANT ALL PRIVILEGES ON *.* TO '{app}'@'%' WITH GRANT OPTION; FLUSH PRIVILEGES;"
    run([*base, "--user", "root", "--password", root_pw, "sql", "-q", grant], env=env)
    typer.echo(f"✓ granted '{app}' full privileges")


def up():
    backend = _backend()
    _ensure_up(backend)
    _compose(backend, "up", "-d")
    provision()


def down():
    _compose(_backend(), "down")


def logs():
    _compose(_backend(), "logs", "-f")


def ps():
    _compose(_backend(), "ps")


def sql():
    backend = _backend()
    run([_runtime(backend), "exec", "-it", "beads-db", "dolt", "sql"])
