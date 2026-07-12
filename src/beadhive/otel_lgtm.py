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

from . import compose, config


def _compose(backend, *args):
    """Run a compose subcommand against the otel-lgtm stack's compose file (shared lifecycle).

    Now carries the ~/.ws/.env overlay via ``compose.run_compose`` — previously this stack ran
    compose with NO env, so it could not see the ports/tokens the env file defines (the bug this
    consolidation fixes)."""
    compose.run_compose(backend, config.otel_compose_file(), "docker-compose.otel.yml", *args)


def up():
    backend = compose.backend()
    compose.ensure_up(backend)
    _compose(backend, "up", "-d")


def down():
    _compose(compose.backend(), "down")


def logs():
    _compose(compose.backend(), "logs", "-f")


def ps():
    _compose(compose.backend(), "ps")
