"""Optional, best-effort integration with the :command:`herdr` terminal server.

This initial plugin deliberately owns no worktree lifecycle.  ``bh`` remains the
authority for managed worktrees and branches; herdr is only an interactive
execution surface.  Every probe in this module is fenced: an absent executable,
stopped server, or failed subprocess must never make importing ``beadhive`` (or
the generic plugin registry) fail.
"""

from __future__ import annotations

import shutil

import typer

from . import plugins, run


def _has_cli() -> bool:
    """Whether the herdr executable is available without raising."""
    try:
        return shutil.which("herdr") is not None
    except Exception:  # noqa: BLE001 - optional integration availability probe
        return False


def _invoke(argv: list[str]):
    """Run a read-only herdr probe, returning ``None`` for every failure."""
    try:
        return run.run(argv, check=False, capture=True)
    except Exception:  # noqa: BLE001 - server may be stopped or the process may fail
        return None


def server_up() -> bool:
    """True exactly when ``herdr status`` can reach a running server.

    The status command is herdr's authority for server health.  Its output is
    intentionally not reimplemented or parsed here, keeping this wrapper stable
    across herdr versions.
    """
    if not _has_cli():
        return False
    result = _invoke(["herdr", "status"])
    return result is not None and result.returncode == 0


def _output(result) -> str:
    """Best-effort text from a captured subprocess result."""
    return str(getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()


cli = typer.Typer(no_args_is_help=True, help="herdr terminal/agent-pane integration.")


@cli.command("status", help="show herdr server health and installed agent integrations.")
def _status_cmd() -> None:
    """A safe one-shot health report for herdr and its per-kind hook integrations."""
    if not _has_cli():
        typer.echo("herdr: server=down (herdr CLI not on PATH)")
        typer.echo("herdr integrations: unavailable")
        return

    status = _invoke(["herdr", "status"])
    if status is None or status.returncode != 0:
        typer.echo("herdr: server=down")
    else:
        typer.echo("herdr: server=up")
        if output := _output(status):
            typer.echo(output)

    integrations = _invoke(["herdr", "integration", "status"])
    if integrations is None or integrations.returncode != 0:
        typer.echo("herdr integrations: unavailable")
    else:
        typer.echo("herdr integrations:")
        if output := _output(integrations):
            typer.echo(output)


PLUGIN = plugins.Plugin(
    name="herdr",
    cli=cli,
    enabled=lambda cfg, entry: server_up(),
)
