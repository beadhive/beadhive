"""Provision and manage the Dolt SQL server behind beads.

Ports scripts/provision.sh + the justfile up/down/logs/ps/sql recipes, with a thin
pluggable container backend (colima | docker | podman | none) selected by
`dolt.backend` in config — start at one real backend with a clean seam, not a
plugin framework.
"""

from __future__ import annotations

import time

import typer

from . import compose, config
from .run import ok, run

# The Dolt image creates the app user AFTER it starts accepting connections, so we
# poll for the user (not just the server). 60×1s covers a cold image start; bump if
# your host is slower.
WAIT_TRIES = 60
WAIT_SLEEP = 1


def _runtime(backend) -> str:
    return "podman" if backend == "podman" else "docker"


def _compose(backend, *args):
    """Run a compose subcommand against the dolt stack's compose file (shared lifecycle)."""
    compose.run_compose(backend, config.compose_file(), "docker-compose.yml", *args)


def provision():
    env = compose.read_env()
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
    backend = compose.backend()
    compose.ensure_up(backend)
    _compose(backend, "up", "-d")
    provision()


def down():
    _compose(compose.backend(), "down")


def logs():
    _compose(compose.backend(), "logs", "-f")


def ps():
    _compose(compose.backend(), "ps")


def sql():
    backend = compose.backend()
    run([_runtime(backend), "exec", "-it", "beads-db", "dolt", "sql"])
