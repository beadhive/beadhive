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
from typing import NoReturn

import typer

from . import config
from .run import ok, run

#: Marker baked into the Beadhive image (docker/Dockerfile). An EXPLICIT signal, not a sniff of
#: /.dockerenv or /proc/1/cgroup: those differ between docker, podman, containerd and nerdctl and
#: have changed shape between versions of each, so a detector built on them goes quietly wrong on
#: whichever runtime nobody tested.
CONTAINER_MARKER = "BH_IN_CONTAINER"

_FALSEY = {"", "0", "false", "no"}


def in_container() -> bool:
    """True when running inside the Beadhive image."""
    return os.environ.get(CONTAINER_MARKER, "").strip().lower() not in _FALSEY


def _refuse_in_container(stack: str) -> NoReturn:
    """Say what to do, instead of failing with a bare ``docker: not found``.

    The user is NOT missing a runtime — anyone running this image has a docker-compatible one on
    the HOST. What they lack is a way to reach it from in here, by design: the host socket is
    deliberately not mounted, because that would hand this container host root.
    """
    typer.echo(
        f"✗ `bh {stack} up` cannot drive a container runtime from inside the Beadhive container.\n"
        "\n"
        "  There is no runtime in here, and the host's docker socket is deliberately NOT\n"
        "  mounted — mounting it would hand this container host root.\n"
        "\n"
        "  Run it from the HOST instead, where your runtime already lives:\n"
        "\n"
        f"      bh {stack} up\n"
        "\n"
        "  The container reaches the stack over the network once it is up; it never needs to\n"
        "  start it. `dolt.backend` already defaults to `none` in this image for that reason.",
        err=True,
    )
    raise typer.Exit(1)


def backend() -> str:
    """The container runtime to drive — defaulting to ``none`` inside the image.

    In-container the honest default is "someone else manages this stack": there is nothing here
    to drive. An explicit ``dolt.backend`` in config still wins, so an operator who knows better
    is never overridden — INCLUDING a value outside the schema's Literal range (e.g. a
    hand-edited `dolt.backend: shared-server`, bh-aidze): this function still returns it
    verbatim, so a compose path would try to exec a binary literally named `shared-server` and
    fail loudly right there. `config.warn_literal_violations_if_needed()` is the earlier signal
    (CLI-seam, every invocation) that catches this BEFORE it gets here; `bh config set` now
    refuses the same bad value outright (`config._validate`).
    """
    configured = config.dolt_cfg().get("backend")
    if configured:
        return str(configured)
    return "none" if in_container() else "colima"


def compose_cmd(backend):
    override = config.dolt_cfg().get("compose")
    if override:
        return override.split() if isinstance(override, str) else list(override)
    if backend == "podman":
        return ["podman", "compose"]
    if ok(["docker", "compose", "version"]):
        return ["docker", "compose"]
    return ["docker-compose"]


def ensure_up(backend, *, stack: str):
    """Backend-specific pre-step to get a container daemon running.

    ``stack`` is keyword-only and has no default on purpose: it exists solely so the
    in-container refusal can name the command the user actually typed, and a default would let a
    new caller silently produce a message about the wrong stack.
    """
    if in_container():
        _refuse_in_container(stack)
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


def run_compose(backend, compose_file, template, *args, stack: str):
    """Seed ``compose_file`` from bundled ``template`` if absent, then run
    ``compose -f <compose_file> <args>`` from the ws home with the ``~/.ws/.env`` overlay applied
    (so BOTH stacks see the DOLT_*/token/port values the env file defines).

    Refuses in-container before touching the filesystem: seeding a compose file that can never
    be run from here would leave confusing state behind."""
    if in_container():
        _refuse_in_container(stack)
    if not compose_file.exists():
        compose_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.template(template), compose_file)
    cmd = compose_cmd(backend) + ["-f", str(compose_file), *args]
    run(cmd, cwd=str(config.home()), env=read_env())
